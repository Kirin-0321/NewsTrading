"""板块语义聚类服务（2026-05-28 ai_source=D 决策点 cluster_method=llm_only）。

业务定位
--------
``dim_sector`` 1510 个板块名里有大量"近义副本"——白酒/白酒Ⅱ/白酒Ⅲ/酿酒概念
其实是同一主题。直接 5 源合并取 Top 20 时 AI 上下文严重浪费。本模块做一次性
聚类把每个板块归到一个 ``dim_sector_group``，``service._read_sectors_top``
按 group_id 同主题取**中位涨幅**那行（决策点 median_def=lower_median）。

调用链::

    tools/cluster_sectors.py
      └─ load_unclustered_sectors()         # 拉所有未聚类板块
      └─ ↓ 主人手动跑 Cursor Task subagent ↓
      └─ apply_clustering(payload)          # subagent JSON → 落库
                ├─ INSERT dim_sector_group
                └─ UPDATE dim_sector.group_id

    services/market/service.py
      └─ _read_sectors_top                  # PARTITION BY group_id 取中位

JSON 协议（subagent 输出格式）::

    {
      "version": "llm-v1",
      "groups": [
        {
          "name": "白酒",
          "ts_codes": ["BK0896.DC", "BK0972.DC", "881273.TI", "885525.TI", ...],
          "notes": "白酒系细分（dc 概念/dc 行业/ths 行业/ths 概念）"
        },
        ...
      ]
    }

ts_codes 必须全部是 dim_sector 中存在的合法代码；name 必须唯一；不在任何
group 里的板块保持 group_id=NULL（SQL 端 fallback 自成一组）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.market.market_db import get_market_db

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SectorRecord:
    """单个板块的聚类输入（仅暴露 LLM 需要的字段）。"""

    ts_code: str
    name: str
    idx_type: str
    src: str


@dataclass
class GroupingResult:
    """``apply_clustering`` 落库结果。"""

    groups_created: int = 0
    groups_skipped: int = 0           # 已存在的组（按 name 去重）
    sectors_assigned: int = 0
    sectors_unchanged: int = 0
    invalid_ts_codes: List[str] = field(default_factory=list)
    duplicate_assignments: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# 读取（subagent 输入数据源）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 查询：聚类后视图（按 group_id 取下中位涨幅）
# ---------------------------------------------------------------------------

#: 决策点 median_def=lower_median
#: ASC 排序后第 (n+1)/2 名（整数除法）= 下中位
#:   n=1 → rn=1 ✓        n=2 → rn=1 (较小者)
#:   n=3 → rn=2 ✓        n=4 → rn=2 (排序后第二小)
#:   n=5 → rn=3 ✓
#:
#: 未聚类的板块（group_id IS NULL）走 'ts_code' 自成一组，保证不丢数据。
#: gkey = 'g'||group_id（已聚类）/ 's'||ts_code（未聚类）
#:
#: 输出列：
#:   ts_code / pct_chg / main_net_yi / main_elg_yi / main_lg_yi / pct_chg_5d
#:   rank_today
#:   sector_name (中位代表板块的原始 dim_sector.name)
#:   group_name  (dim_sector_group.name，未聚类时为 NULL)
#:   display_name (优先 group_name，未聚类用 sector_name)
#:   cnt_in_data  (当日有数据的组员数，是聚合显示的"×N")
#:   members_count (dim_sector_group 登记的总组员数，未聚类=NULL)
_SECTOR_GROUPED_MEDIAN_SQL = """
WITH base AS (
    SELECT s.ts_code, s.pct_chg, s.main_net_yi, s.main_elg_yi,
           s.main_lg_yi, s.pct_chg_5d, s.rank_today,
           s.total_mv, s.turnover_rate, s.up_num, s.down_num,
           d.name AS sector_name, d.idx_type, d.src,
           d.group_id,
           g.name AS group_name,
           g.members_count
    FROM fact_sector_daily s
    LEFT JOIN dim_sector d ON d.ts_code = s.ts_code
    LEFT JOIN dim_sector_group g ON g.id = d.group_id
    WHERE s.trade_date = ?
), keyed AS (
    SELECT *,
           COALESCE('g' || group_id, 's' || ts_code) AS gkey
    FROM base
), ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY gkey
               ORDER BY pct_chg ASC NULLS LAST, ts_code
           ) AS rn,
           COUNT(*) OVER (PARTITION BY gkey) AS cnt_in_data
    FROM keyed
), dc_fallback AS (
    -- 决策 5：每组从组内 dc 源板块取 4 字段
    -- （ths 源板块不提供 total_mv/turnover_rate/up_num/down_num，
    --  中位代表恰为 ths 时用此 fallback 补值；整组只有 ths 时仍 NULL）
    SELECT gkey,
           MAX(CASE WHEN src='dc' THEN total_mv      END) AS dc_total_mv,
           MAX(CASE WHEN src='dc' THEN turnover_rate END) AS dc_turnover_rate,
           MAX(CASE WHEN src='dc' THEN up_num        END) AS dc_up_num,
           MAX(CASE WHEN src='dc' THEN down_num      END) AS dc_down_num
    FROM keyed
    GROUP BY gkey
)
SELECT r.ts_code, r.pct_chg, r.main_net_yi, r.main_elg_yi,
       r.main_lg_yi, r.pct_chg_5d, r.rank_today,
       r.sector_name, r.group_name,
       COALESCE(r.group_name, r.sector_name) AS display_name,
       r.cnt_in_data,
       r.members_count,
       COALESCE(r.total_mv,      f.dc_total_mv)      AS total_mv,
       COALESCE(r.turnover_rate, f.dc_turnover_rate) AS turnover_rate,
       COALESCE(r.up_num,        f.dc_up_num)        AS up_num,
       COALESCE(r.down_num,      f.dc_down_num)      AS down_num
FROM ranked r
LEFT JOIN dc_fallback f USING (gkey)
WHERE r.rn = (r.cnt_in_data + 1) / 2
ORDER BY r.pct_chg DESC NULLS LAST
"""


def query_grouped_sectors_for_date(
    trade_date: str,
) -> List[Dict[str, Any]]:
    """按 dim_sector_group 聚合后取每组下中位涨幅板块。

    用于 GUI「聚类后视图」+ AI 「Top N 板块榜」去冗余。

    每个组只返回 1 行：
      - 已聚类的组（dim_sector.group_id IS NOT NULL）→ 按 group_id 分组
      - 未聚类板块（group_id IS NULL）→ 每个板块自成一组（不丢数据）

    "下中位"定义（决策点 median_def=lower_median）：
      ASC 排序后第 (n+1)/2 个（n=2 时取较小者，n=3 时取中间）

    Args:
        trade_date: ``YYYYMMDD`` 必传

    Returns:
        list[dict] 每行包含：

          - ts_code, pct_chg, main_net_yi, main_elg_yi, main_lg_yi
          - pct_chg_5d, rank_today
          - **total_mv** (亿元，2026-05-28 新增；中位 ths 时由 dc fallback 补)
          - **turnover_rate** (%，同上)
          - **up_num / down_num** (int，同上)
          - sector_name (中位代表板块原始名)
          - group_name (NULL 表示未聚类)
          - display_name (优先 group_name，NULL 时用 sector_name)
          - cnt_in_data (当日数据中组员数)
          - members_count (dim_sector_group 登记总成员数)

        按 pct_chg DESC 排序。
    """
    if not trade_date:
        return []
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            _SECTOR_GROUPED_MEDIAN_SQL, (trade_date,),
        ).fetchall()
    return [
        {
            "ts_code": str(r["ts_code"]),
            "pct_chg": (
                float(r["pct_chg"]) if r["pct_chg"] is not None else None
            ),
            "main_net_yi": (
                float(r["main_net_yi"])
                if r["main_net_yi"] is not None else None
            ),
            "main_elg_yi": (
                float(r["main_elg_yi"])
                if r["main_elg_yi"] is not None else None
            ),
            "main_lg_yi": (
                float(r["main_lg_yi"])
                if r["main_lg_yi"] is not None else None
            ),
            "pct_chg_5d": (
                float(r["pct_chg_5d"])
                if r["pct_chg_5d"] is not None else None
            ),
            "rank_today": (
                int(r["rank_today"])
                if r["rank_today"] is not None else None
            ),
            "total_mv": (
                float(r["total_mv"]) if r["total_mv"] is not None else None
            ),
            "turnover_rate": (
                float(r["turnover_rate"])
                if r["turnover_rate"] is not None else None
            ),
            "up_num": (
                int(r["up_num"]) if r["up_num"] is not None else None
            ),
            "down_num": (
                int(r["down_num"]) if r["down_num"] is not None else None
            ),
            "sector_name": str(r["sector_name"] or ""),
            "group_name": (
                str(r["group_name"]) if r["group_name"] else None
            ),
            "display_name": str(r["display_name"] or ""),
            "cnt_in_data": int(r["cnt_in_data"] or 0),
            "members_count": (
                int(r["members_count"])
                if r["members_count"] is not None else None
            ),
        }
        for r in rows
    ]


#: 拿"全部成员"用的 SQL（不做中位筛选，留 Python 端聚合 + 计算中位）
_SECTOR_GROUPED_FULL_SQL = """
SELECT s.ts_code, s.pct_chg, s.main_net_yi, s.main_elg_yi,
       s.main_lg_yi, s.pct_chg_5d, s.rank_today,
       s.total_mv, s.turnover_rate, s.up_num, s.down_num,
       d.name AS sector_name, d.idx_type, d.src,
       d.group_id,
       g.name AS group_name,
       g.members_count
FROM fact_sector_daily s
LEFT JOIN dim_sector d ON d.ts_code = s.ts_code
LEFT JOIN dim_sector_group g ON g.id = d.group_id
WHERE s.trade_date = ?
"""


def query_grouped_sectors_with_members(
    trade_date: str,
) -> List[Dict[str, Any]]:
    """聚类后视图（含成员明细）——给 GUI QTreeWidget 用。

    每个组返回 1 个嵌套 dict：

      - **顶层聚合**：组名 + 中位涨幅 + 中位主力 + 当日组员数 + 登记组员数
      - **members[]**：组内**当日有数据**的全部成员明细，含 is_median 标记

    "下中位"取值规则（ASC 排序后第 (n+1)/2 个）：
      n=1 → 唯一行；n=2 → 较小；n=3 → 中间；n=4 → 第二小；n=5 → 第三小

    Args:
        trade_date: ``YYYYMMDD`` 必传

    Returns:
        list[dict] 按 median_pct_chg DESC 排序，每项结构::

            {
              "group_id": int | None,           # None=未聚类自成一组
              "group_name": str | None,         # NULL=未聚类
              "display_name": str,              # group_name 或 sector_name
              "members_count": int | None,      # dim_sector_group 登记
              "cnt_in_data": int,               # 当日实际数据条数

              # 中位代表（用于聚合行显示）
              "median_ts_code": str,
              "median_sector_name": str,
              "median_pct_chg": float | None,
              "median_pct_chg_5d": float | None,
              "median_main_net_yi": float | None,
              "median_main_elg_yi": float | None,
              "median_main_lg_yi": float | None,
              "median_rank_today": int | None,

              # 全部成员（含中位代表自身）
              "members": [
                  {
                      "ts_code", "sector_name", "idx_type", "src",
                      "pct_chg", "pct_chg_5d",
                      "main_net_yi", "main_elg_yi", "main_lg_yi",
                      "rank_today", "is_median"
                  },
                  ...
              ]
            }
    """
    if not trade_date:
        return []

    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            _SECTOR_GROUPED_FULL_SQL, (trade_date,),
        ).fetchall()

    # 按 gkey 分桶
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    bucket_meta: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        gid = r["group_id"]
        ts = str(r["ts_code"])
        gkey = f"g{gid}" if gid is not None else f"s{ts}"

        m = {
            "ts_code": ts,
            "sector_name": str(r["sector_name"] or ""),
            "idx_type": str(r["idx_type"] or ""),
            "src": str(r["src"] or ""),
            "pct_chg": (
                float(r["pct_chg"]) if r["pct_chg"] is not None else None
            ),
            "pct_chg_5d": (
                float(r["pct_chg_5d"])
                if r["pct_chg_5d"] is not None else None
            ),
            "main_net_yi": (
                float(r["main_net_yi"])
                if r["main_net_yi"] is not None else None
            ),
            "main_elg_yi": (
                float(r["main_elg_yi"])
                if r["main_elg_yi"] is not None else None
            ),
            "main_lg_yi": (
                float(r["main_lg_yi"])
                if r["main_lg_yi"] is not None else None
            ),
            "total_mv": (
                float(r["total_mv"]) if r["total_mv"] is not None else None
            ),
            "turnover_rate": (
                float(r["turnover_rate"])
                if r["turnover_rate"] is not None else None
            ),
            "up_num": (
                int(r["up_num"]) if r["up_num"] is not None else None
            ),
            "down_num": (
                int(r["down_num"]) if r["down_num"] is not None else None
            ),
            "rank_today": (
                int(r["rank_today"])
                if r["rank_today"] is not None else None
            ),
        }
        buckets.setdefault(gkey, []).append(m)
        if gkey not in bucket_meta:
            bucket_meta[gkey] = {
                "group_id": int(gid) if gid is not None else None,
                "group_name": (
                    str(r["group_name"]) if r["group_name"] else None
                ),
                "members_count": (
                    int(r["members_count"])
                    if r["members_count"] is not None else None
                ),
            }

    out: List[Dict[str, Any]] = []
    for gkey, members in buckets.items():
        meta = bucket_meta[gkey]

        # 下中位：按 pct_chg ASC, ts_code 排序后第 (n+1)/2 项（1-based）
        sorted_members = sorted(
            members,
            key=lambda m: (
                m["pct_chg"] if m["pct_chg"] is not None else float("inf"),
                m["ts_code"],
            ),
        )
        n = len(sorted_members)
        median_idx = (n + 1) // 2 - 1  # 0-based
        median = sorted_members[median_idx]
        # 标记 is_median
        median_ts = median["ts_code"]
        for m in members:
            m["is_median"] = (m["ts_code"] == median_ts)

        # 组内显示按 pct_chg DESC（更直观）
        members_for_display = sorted(
            members,
            key=lambda m: (
                -(m["pct_chg"] if m["pct_chg"] is not None else -float("inf")),
                m["ts_code"],
            ),
        )

        display_name = (
            meta["group_name"] or median["sector_name"]
        )
        # 决策 5（2026-05-28）：dc fallback —— 中位代表是 ths 源时，
        # 4 字段全 NULL，用组内任一 dc 源板块的 4 字段顶替；整组只有 ths
        # 时仍为 NULL（GUI 显示 "—"）。
        dc_fallback = next(
            (mm for mm in members if mm.get("src") == "dc"),
            None,
        )

        def _coalesce(field: str) -> Any:
            v = median.get(field)
            if v is not None:
                return v
            return dc_fallback.get(field) if dc_fallback else None

        out.append({
            "group_id": meta["group_id"],
            "group_name": meta["group_name"],
            "display_name": display_name,
            "members_count": meta["members_count"],
            "cnt_in_data": n,
            "median_ts_code": median["ts_code"],
            "median_sector_name": median["sector_name"],
            "median_pct_chg": median["pct_chg"],
            "median_pct_chg_5d": median["pct_chg_5d"],
            "median_main_net_yi": median["main_net_yi"],
            "median_main_elg_yi": median["main_elg_yi"],
            "median_main_lg_yi": median["main_lg_yi"],
            "median_total_mv": _coalesce("total_mv"),
            "median_turnover_rate": _coalesce("turnover_rate"),
            "median_up_num": _coalesce("up_num"),
            "median_down_num": _coalesce("down_num"),
            "median_rank_today": median["rank_today"],
            "members": members_for_display,
        })

    out.sort(
        key=lambda g: (
            -(g["median_pct_chg"]
              if g["median_pct_chg"] is not None else -float("inf")),
            g["display_name"],
        ),
    )
    return out


def count_grouped_sectors_for_date(trade_date: str) -> int:
    """统计某交易日聚类后剩多少行（GUI ComboBox 标签用）。

    口径与 :func:`query_grouped_sectors_for_date` 一致——已聚类按 group
    计数 + 未聚类按 ts_code 单独计数。
    """
    if not trade_date:
        return 0
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE('g'||d.group_id, 's'||s.ts_code)) "
            "FROM fact_sector_daily s "
            "LEFT JOIN dim_sector d ON d.ts_code = s.ts_code "
            "WHERE s.trade_date = ?",
            (trade_date,),
        ).fetchone()
    return int(row[0] or 0)


def load_unclustered_sectors() -> List[SectorRecord]:
    """返回当前 ``group_id IS NULL`` 的全部板块。

    第一次跑时 = 全表 1510 行；后续增量跑时 = 新增板块（Tushare 偶尔加新概念）。
    """
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts_code, name, idx_type, src "
            "FROM dim_sector "
            "WHERE group_id IS NULL "
            "  AND idx_type IS NOT NULL "
            "ORDER BY idx_type, name"
        ).fetchall()
    return [
        SectorRecord(
            ts_code=str(r["ts_code"]),
            name=str(r["name"]),
            idx_type=str(r["idx_type"]),
            src=str(r["src"]),
        )
        for r in rows
    ]


def export_for_subagent(out_path: Path) -> int:
    """把未聚类板块清单导出 JSON，供 Cursor Task subagent 读取。

    输出 JSON 结构::

        {
          "trade_date_snapshot": "20260527",
          "total": 1510,
          "instructions": "...给 subagent 看的指令",
          "sectors": [
            {"ts_code": "...", "name": "...", "idx_type": "...", "src": "..."},
            ...
          ]
        }

    Returns:
        写入的板块行数
    """
    sectors = load_unclustered_sectors()
    payload = {
        "schema_version": "1.0",
        "total": len(sectors),
        "instructions": (
            "你是金融题材聚类助手。下面 sectors[] 是 A 股各类板块指数（共"
            f" {len(sectors)} 个），来源包括东方财富(dc)和同花顺(ths)的概念/"
            "行业/地域指数。请按【投资主题】把名义不同但语义相同的板块聚到"
            "同一组里，目标是把 1500 个名压成 600~800 个独立主题组。\n\n"
            "聚类原则：\n"
            "  ① 同名板块（'白酒概念' dc + 'ths'）必须同组\n"
            "  ② 同根细分（白酒/白酒Ⅱ/白酒Ⅲ/酿酒概念）必须同组，组名取最简\n"
            "  ③ 紧密相关概念（'AIGC概念'/'AI应用'/'ChatGPT概念'）合理同组\n"
            "  ④ 跨产业不要硬合并（'锂电池'≠'氢能源'，纵然都算新能源）\n"
            "  ⑤ 含'指数/Ⅱ/Ⅲ/概念/板块'后缀的等价于其前缀\n"
            "  ⑥ 不要漏一个 ts_code（单成员组也允许）\n"
            "  ⑦ 同一 ts_code 不能出现在多个组里\n\n"
            "输出严格 JSON 到 stdout，结构：\n"
            "{\n"
            '  "version": "llm-v1",\n'
            '  "groups": [\n'
            '    {"name": "白酒", "ts_codes": ["BK0896.DC", "881273.TI", ...], "notes": "..."},\n'
            "    ...\n"
            "  ]\n"
            "}\n\n"
            "name 字段限 1~20 字符；notes 字段可选，简短说明该组涵盖范围。"
        ),
        "sectors": [
            {
                "ts_code": s.ts_code,
                "name": s.name,
                "idx_type": s.idx_type,
                "src": s.src,
            }
            for s in sectors
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(sectors)


# ---------------------------------------------------------------------------
# 写入（subagent 输出 → 落库）
# ---------------------------------------------------------------------------


#: idx_type → 中文消歧后缀（用于自动加在重名组上）
_IDX_TYPE_TAG = {
    "concept": "概念",
    "industry": "行业",
    "region": "地域",
}

#: src → 中文消歧后缀
_SRC_TAG = {
    "dc": "DC",
    "ths": "THS",
}


def _suffix_for_group(
    ts_codes: List[str],
    sector_meta: Dict[str, Tuple[str, str]],
) -> str:
    """根据组内 ts_codes 推断消歧后缀（重名时用）。

    优先级：
      1. 全部 ts_code 同 idx_type+src → "（DC行业）"
      2. 全部同 idx_type 但跨源 → "（行业）"
      3. 全部同 src 但跨 idx_type → "（DC）"
      4. 完全混合 → "（混合）"
    """
    src_set = {sector_meta.get(c, ("", ""))[0] for c in ts_codes}
    type_set = {sector_meta.get(c, ("", ""))[1] for c in ts_codes}
    src_set.discard("")
    type_set.discard("")

    src_part = ""
    if len(src_set) == 1:
        src_part = _SRC_TAG.get(next(iter(src_set)), "")
    type_part = ""
    if len(type_set) == 1:
        type_part = _IDX_TYPE_TAG.get(next(iter(type_set)), "")

    label = f"{src_part}{type_part}" or "混合"
    return f"（{label}）"


def _validate_payload(
    payload: Dict[str, Any],
    valid_ts_codes: set,
    sector_meta: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """校验 payload 的 groups 字段，返回 (valid_groups, error_messages)。

    校验项：
      - schema 必备字段
      - 每组 name 1~50 字符
      - ts_codes 全部在字典里存在
      - 同一 ts_code 不重复出现
      - 重名组自动加来源后缀消歧（不再丢弃）

    Args:
        sector_meta: ts_code → (src, idx_type)，给重名组加后缀用

    Returns:
        (valid_groups, errors, info)
        - errors 是真正的 fatal（schema 错 / ts_code 编造等）
        - info 是 INFO 级提示（如自动消歧），不影响落库
    """
    errors: List[str] = []
    info: List[str] = []
    if not isinstance(payload, dict):
        return [], ["payload 不是 dict"], info
    groups = payload.get("groups")
    if not isinstance(groups, list):
        return [], ["payload.groups 不是 list"], info

    sector_meta = sector_meta or {}
    seen_codes: Dict[str, str] = {}     # ts_code → 已分配的最终组名
    seen_names: set = set()
    valid_groups: List[Dict[str, Any]] = []
    rename_log: List[str] = []          # 记录被重命名的组（debug 用）

    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            errors.append(f"groups[{i}] 不是 dict")
            continue
        raw_name = (g.get("name") or "").strip()
        if not (1 <= len(raw_name) <= 50):
            errors.append(f"groups[{i}] name 长度非法: {raw_name!r}")
            continue

        codes = g.get("ts_codes") or []
        if not isinstance(codes, list) or not codes:
            errors.append(f"groups[{i}] ({raw_name!r}) ts_codes 为空")
            continue

        valid_codes: List[str] = []
        for code in codes:
            code = str(code).strip()
            if code not in valid_ts_codes:
                errors.append(
                    f"groups[{i}] ({raw_name!r}) ts_code 不在字典: {code!r}"
                )
                continue
            if code in seen_codes:
                errors.append(
                    f"ts_code 重复出现: {code!r} 同时在 "
                    f"{seen_codes[code]!r} 和 {raw_name!r}"
                )
                continue
            valid_codes.append(code)
        if not valid_codes:
            continue

        # 重名自动加来源后缀消歧
        name = raw_name
        if name in seen_names:
            suffix = _suffix_for_group(valid_codes, sector_meta)
            name = f"{raw_name}{suffix}"
            # 极端情况：连后缀都重了（不太可能，但兜底加序号）
            n_seq = 2
            base = name
            while name in seen_names:
                name = f"{base}-{n_seq}"
                n_seq += 1
            rename_log.append(f"{raw_name!r} → {name!r}")

        for code in valid_codes:
            seen_codes[code] = name
        rationale = (
            g.get("rationale") or g.get("notes") or ""
        ).strip()[:300]
        valid_groups.append({
            "name": name,
            "ts_codes": valid_codes,
            "notes": rationale,
        })
        seen_names.add(name)

    if rename_log:
        info.append(
            f"自动消歧 {len(rename_log)} 个重名组（前 5: "
            + "; ".join(rename_log[:5]) + "）"
        )

    return valid_groups, errors, info


def apply_clustering(
    payload: Dict[str, Any],
    *,
    created_by: str = "llm-v1",
    dry_run: bool = False,
) -> GroupingResult:
    """把 subagent 输出的 JSON 落库。

    做了什么：
      1. 校验 payload 结构与 ts_code 合法性
      2. 对每个 group：INSERT INTO dim_sector_group（name 唯一冲突时复用旧行）
      3. UPDATE dim_sector.group_id 给该组成员
      4. 没在任何组里的 ts_code 保持 group_id=NULL

    幂等：同名组重跑会复用旧 id 并 UPDATE 成员；不会重复落 group。

    dry_run=True：完成校验和数量统计但不写库（用于 --dry-run 预览）。
    """
    result = GroupingResult()
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        valid_codes_set = {
            str(r["ts_code"]) for r in conn.execute(
                "SELECT ts_code FROM dim_sector WHERE idx_type IS NOT NULL"
            ).fetchall()
        }
        all_records = [
            SectorRecord(
                ts_code=str(r["ts_code"]),
                name=str(r["name"]),
                idx_type=str(r["idx_type"]),
                src=str(r["src"]),
            )
            for r in conn.execute(
                "SELECT ts_code, name, idx_type, src FROM dim_sector "
                "WHERE idx_type IS NOT NULL"
            ).fetchall()
        ]

    # 二重保险：落库前再过一次质量闸门，挡住手工直接 apply 一份偷懒 JSON
    quality = _assess_quality(payload, all_records)
    if quality["fatals"]:
        result.errors.append("落库被拦：质量闸门未过")
        result.errors.extend(quality["fatals"])
        return result

    sector_meta: Dict[str, Tuple[str, str]] = {
        r.ts_code: (r.src, r.idx_type) for r in all_records
    }
    valid_groups, errs, info_msgs = _validate_payload(
        payload, valid_codes_set, sector_meta=sector_meta,
    )
    result.errors.extend(errs)
    # info 不进 errors（不是失败信号），但通过 duplicate_assignments 透出
    if info_msgs:
        result.duplicate_assignments.extend(info_msgs)
    if not valid_groups:
        result.errors.append("没有合法的组")
        return result

    if dry_run:
        result.groups_created = len(valid_groups)
        result.sectors_assigned = sum(
            len(g["ts_codes"]) for g in valid_groups
        )
        return result

    with db.connect() as conn:
        for g in valid_groups:
            existing = conn.execute(
                "SELECT id FROM dim_sector_group WHERE name = ?",
                (g["name"],),
            ).fetchone()
            if existing:
                gid = int(existing["id"])
                result.groups_skipped += 1
                conn.execute(
                    "UPDATE dim_sector_group "
                    "SET members_count = ?, notes = COALESCE(?, notes) "
                    "WHERE id = ?",
                    (len(g["ts_codes"]), g.get("notes") or None, gid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO dim_sector_group "
                    "(name, members_count, created_by, notes) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        g["name"], len(g["ts_codes"]),
                        created_by, g.get("notes") or None,
                    ),
                )
                gid = int(cur.lastrowid)
                result.groups_created += 1

            for code in g["ts_codes"]:
                cur = conn.execute(
                    "UPDATE dim_sector SET group_id = ? "
                    "WHERE ts_code = ?",
                    (gid, code),
                )
                if cur.rowcount > 0:
                    result.sectors_assigned += 1

    return result


def load_groups_with_members() -> List[Dict[str, Any]]:
    """返回每组完整信息 + 成员列表，按 (members_count DESC, name) 排序。

    Returns:
        [
          {
            "id", "name", "members_count", "notes",
            "members": [{"ts_code", "name", "idx_type", "src"}, ...]
          },
          ...
        ]

    未聚类的板块（group_id IS NULL）不在此列表里，调用方需另外查 dim_sector
    WHERE group_id IS NULL 单独显示。
    """
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        groups = conn.execute(
            "SELECT id, name, members_count, notes "
            "FROM dim_sector_group "
            "ORDER BY members_count DESC, name"
        ).fetchall()
        members_rows = conn.execute(
            "SELECT group_id, ts_code, name, idx_type, src "
            "FROM dim_sector "
            "WHERE group_id IS NOT NULL AND idx_type IS NOT NULL "
            "ORDER BY group_id, src, ts_code"
        ).fetchall()

    members_by_gid: Dict[int, List[Dict[str, str]]] = {}
    for r in members_rows:
        members_by_gid.setdefault(int(r["group_id"]), []).append({
            "ts_code": str(r["ts_code"]),
            "name": str(r["name"]),
            "idx_type": str(r["idx_type"]),
            "src": str(r["src"]),
        })

    return [
        {
            "id": int(g["id"]),
            "name": str(g["name"]),
            "members_count": int(g["members_count"]),
            "notes": str(g["notes"] or ""),
            "members": members_by_gid.get(int(g["id"]), []),
        }
        for g in groups
    ]


def load_unclustered_members() -> List[Dict[str, str]]:
    """未聚类板块清单（group_id IS NULL）。"""
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts_code, name, idx_type, src "
            "FROM dim_sector "
            "WHERE group_id IS NULL AND idx_type IS NOT NULL "
            "ORDER BY src, ts_code"
        ).fetchall()
    return [
        {
            "ts_code": str(r["ts_code"]),
            "name": str(r["name"]),
            "idx_type": str(r["idx_type"]),
            "src": str(r["src"]),
        }
        for r in rows
    ]


def export_groups_to_markdown(
    out_path: Path,
    *,
    flat: bool = False,
) -> Dict[str, int]:
    """把 dim_sector_group 全部组及成员导出 markdown，给主人审阅用。

    Args:
        flat: True = 扁平表格（每行一板块，含分组名）；
              False = 按组分章节（默认）

    扁平输出结构：
      统计概览 + 一张包含 1510 行板块的大表（分组名/原始名/ts_code/idx_type/src）

    分组输出结构：
      一、统计概览（组数、平均、单成员、最大组）
      二、按 members_count 倒序逐组列出（组名、rationale、成员表）
      三、未聚类板块（group_id IS NULL）

    Returns:
        {"groups": <组数>, "members": <已聚类板块数>, "unclustered": <未聚类>}
    """
    groups = load_groups_with_members()
    unclustered = load_unclustered_members()
    if flat:
        return _export_flat_md(out_path, groups, unclustered)

    total_members = sum(g["members_count"] for g in groups)
    singletons = sum(1 for g in groups if g["members_count"] == 1)
    biggest = groups[0] if groups else None
    avg = total_members / len(groups) if groups else 0

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = []
    lines.append("# 板块语义聚类·成员清单（审阅用）")
    lines.append("")
    lines.append(f"> 生成时间：{now}  ")
    lines.append(f"> 数据来源：`dim_sector_group` + `dim_sector`  ")
    lines.append(
        f"> 决策点 ai_source=D + cluster_method=llm_only "
        f"+ median_def=lower_median"
    )
    lines.append("")

    lines.append("## 一、统计概览")
    lines.append("")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 总组数 | {len(groups)} |")
    lines.append(f"| 已聚类板块 | {total_members} |")
    lines.append(f"| 未聚类板块 | {len(unclustered)} |")
    lines.append(f"| 平均组大小 | {avg:.2f} |")
    lines.append(f"| 单成员组数 | {singletons} ({singletons/len(groups)*100:.1f}% 总组数)" if groups else "| 单成员组数 | 0 |")
    if biggest:
        lines.append(
            f"| 最大组 | {biggest['name']} (×{biggest['members_count']}) |"
        )
    lines.append("")

    lines.append("**最大 20 组**：")
    lines.append("")
    lines.append("| 组名 | 成员数 | rationale |")
    lines.append("|------|------:|------|")
    for g in groups[:20]:
        rat = g["notes"].replace("|", "/").replace("\n", " ")[:60]
        lines.append(
            f"| {g['name']} | {g['members_count']} | {rat} |"
        )
    lines.append("")

    lines.append("## 二、全部组明细（按成员数降序）")
    lines.append("")

    for g in groups:
        anchor = f"group-{g['id']}"
        lines.append(
            f"### <a id=\"{anchor}\"></a>{g['name']} "
            f"(×{g['members_count']})  `id={g['id']}`"
        )
        lines.append("")
        if g["notes"]:
            lines.append(f"> **rationale**: {g['notes']}")
            lines.append("")
        if not g["members"]:
            lines.append("_⚠ 数据库中无成员（孤儿组，可能被合并后未清理）_")
            lines.append("")
            continue
        lines.append("| ts_code | 原始名称 | idx_type | src |")
        lines.append("|---------|---------|----------|-----|")
        for m in g["members"]:
            n = m["name"].replace("|", "/")
            lines.append(
                f"| `{m['ts_code']}` | {n} | {m['idx_type']} | {m['src']} |"
            )
        lines.append("")

    if unclustered:
        lines.append("## 三、未聚类板块（group_id IS NULL）")
        lines.append("")
        lines.append(
            f"以下 {len(unclustered)} 个板块在一阶段聚类时被 LLM 漏掉，"
            "落库时保持 group_id=NULL，SQL 查询时自成一组（不会丢失数据）。"
        )
        lines.append("")
        lines.append("| ts_code | 名称 | idx_type | src |")
        lines.append("|---------|------|----------|-----|")
        for m in unclustered:
            n = m["name"].replace("|", "/")
            lines.append(
                f"| `{m['ts_code']}` | {n} | {m['idx_type']} | {m['src']} |"
            )
        lines.append("")
    else:
        lines.append("## 三、未聚类板块")
        lines.append("")
        lines.append("✅ 全部板块均已分组。")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "groups": len(groups),
        "members": total_members,
        "unclustered": len(unclustered),
    }


def _export_flat_md(
    out_path: Path,
    groups: List[Dict[str, Any]],
    unclustered: List[Dict[str, str]],
) -> Dict[str, int]:
    """扁平表格视图：每行一个原始板块，列出归属分组名 + 成员数。

    主用途：主人想一眼扫完全部 1510 个板块在哪个分组，或者按 ts_code
    查找单个板块归属。
    """
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_members = sum(g["members_count"] for g in groups)
    flat_rows: List[Tuple[str, int, Dict[str, str]]] = []
    for g in groups:
        for m in g["members"]:
            flat_rows.append((g["name"], g["members_count"], m))

    flat_rows.sort(key=lambda r: (r[0], r[2]["src"], r[2]["ts_code"]))

    lines: List[str] = []
    lines.append("# 全部板块·分组归属扁平表")
    lines.append("")
    lines.append(f"> 生成时间：{now}  ")
    lines.append(
        f"> 共 {total_members + len(unclustered)} 个板块"
        f"（已分组 {total_members} + 未分组 {len(unclustered)}），"
        f"分布在 {len(groups)} 组里"
    )
    lines.append(
        "> 按 (分组名, src, ts_code) 排序，方便按字典序浏览或全文搜板块代码"
    )
    lines.append("")

    lines.append("## 一、统计概览")
    lines.append("")
    biggest = groups[0] if groups else None
    singletons = sum(1 for g in groups if g["members_count"] == 1)
    avg = total_members / len(groups) if groups else 0
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 总组数 | {len(groups)} |")
    lines.append(f"| 已聚类板块 | {total_members} |")
    lines.append(f"| 未聚类板块 | {len(unclustered)} |")
    lines.append(f"| 平均组大小 | {avg:.2f} |")
    if groups:
        lines.append(
            f"| 单成员组数 | {singletons} ({singletons/len(groups)*100:.1f}%) |"
        )
    if biggest:
        lines.append(
            f"| 最大组 | {biggest['name']} (×{biggest['members_count']}) |"
        )
    lines.append("")

    lines.append("## 二、全部板块列表")
    lines.append("")
    lines.append(
        "| # | 分组名 | 组员数 | 原始名称 | ts_code | idx_type | src |"
    )
    lines.append(
        "|--:|------|------:|---------|---------|----------|-----|"
    )
    for idx, (gname, gcount, m) in enumerate(flat_rows, 1):
        n = m["name"].replace("|", "/")
        gn = gname.replace("|", "/")
        lines.append(
            f"| {idx} | **{gn}** | {gcount} | {n} | "
            f"`{m['ts_code']}` | {m['idx_type']} | {m['src']} |"
        )
    lines.append("")

    if unclustered:
        lines.append("## 三、未聚类板块（group_id IS NULL）")
        lines.append("")
        lines.append("| 原始名称 | ts_code | idx_type | src |")
        lines.append("|---------|---------|----------|-----|")
        for m in unclustered:
            n = m["name"].replace("|", "/")
            lines.append(
                f"| {n} | `{m['ts_code']}` | {m['idx_type']} | {m['src']} |"
            )
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "groups": len(groups),
        "members": total_members,
        "unclustered": len(unclustered),
    }


def dump_merge_pass_prompt(out_path: Path) -> Dict[str, int]:
    """把"喂给任意 LLM 做深度合并"所需的完整提示词 + 原始数据写进一个 md 文件。

    用途：
      - 主人想换个 AI（Claude / GPT / Gemini）做深度合并
      - 把整份文件（含 system + user prompt + 数据）整体复制给 AI
      - AI 输出 JSON 存到 .huiye/_sector_merge_output.json
      - 跑 `python tools/cluster_sectors.py merge-pass --no-llm --apply`

    Returns:
        {"groups": <组数>, "members": <已聚类板块数>, "tokens_estimate": <估算>}
    """
    groups = load_groups_with_members()
    if not groups:
        raise RuntimeError("dim_sector_group 为空，先跑 cluster")

    lines: List[str] = []

    lines.append(
        "# 板块语义聚类·深度合并任务（整份内容喂给任意 LLM）"
    )
    lines.append("")
    lines.append(
        "> **使用说明**：把本文件**完整内容**（含本说明之下全部）"
        "复制粘贴到任意 LLM（Claude / GPT / Gemini / DeepSeek 等）的对话框，"
        "让它输出 merges JSON 块。"
    )
    lines.append(
        "> 1. 把 LLM 输出的 JSON（仅 `{...}` 部分）保存到 "
        "`.huiye/_sector_merge_output.json`"
    )
    lines.append(
        "> 2. 跑 `python tools/cluster_sectors.py merge-pass --no-llm --apply`"
    )
    lines.append(
        "> 3. 跑 `python tools/cluster_sectors.py export-md` "
        "重新生成审阅清单看效果"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## SYSTEM PROMPT")
    lines.append("")
    lines.append("```")
    lines.append(
        "你是 A 股投资题材深度合并专员。"
        "输入是 dim_sector_group 表里所有 826 组（已经过一阶段保守拆分 +"
        "二阶段近义合并），但仍有大量同产业链不同环节的组没合并。"
        "你的任务：基于每组的【成员明细】判断哪些组应进一步合并到同一主题。"
    )
    lines.append("```")
    lines.append("")

    lines.append("## USER PROMPT")
    lines.append("")
    lines.append("### 合并原则（按优先级，违反任何一条本次输出作废）")
    lines.append("")
    lines.append(
        "1. **同产业链不同环节可合并**：例如汽车整车/乘用车/商用车/电动乘用车"
        " → target=\"汽车整车\""
    )
    lines.append(
        "2. **同主题不同表述可合并**：托育服务/婴童概念/三胎概念 → target=\"三胎\""
    )
    lines.append(
        "3. **「百度概念」「华为概念」等公司股不要被产业链组吃掉**："
        "如果某组主成员是「百度概念」+「无人驾驶」，应**保留独立**，"
        "因为它实际混杂了公司股和主题股；建议主人 split 后再分别合并。"
        "**所以这种混杂组不要 merge**。"
    )
    lines.append(
        "4. **跨产业绝不合并**：锂电池 ≠ 氢能源 ≠ 储能；半导体 ≠ 5G"
    )
    lines.append(
        "5. **target 必须 ≤ 12 字符，无「概念/板块/指数」后缀**"
    )
    lines.append(
        "6. **每个 source name 只能出现在一处合并里**（不重）"
    )
    lines.append(
        "7. **rationale 强制必填**（5~40 字说明合并理由）"
    )
    lines.append(
        "8. **target 不能出现在自己的 sources 里**"
    )
    lines.append(
        "9. **没把握就不合并**——放过近义比错并语义不同的组好得多"
    )
    lines.append(
        "10. **目标**：826 → 600~700 组（粗算合并 ~150~200 条）"
    )
    lines.append("")
    lines.append("### 反例（绝对禁止）")
    lines.append("")
    lines.append(
        "- ❌ 把「百度概念」组吃到「无人驾驶」组里 "
        "（百度概念组里只有 1/3 是无人驾驶相关，其余是百度产品）"
    )
    lines.append(
        "- ❌ 把「华为概念」吃到「智能驾驶」（华为只有华为汽车跟智驾相关）"
    )
    lines.append(
        "- ❌ 把整车跟零部件合并（产业链上下游，业绩不同）"
    )
    lines.append(
        "- ❌ 编造不在下方清单里的组名（target 例外可创新名，但 sources 不许）"
    )
    lines.append("")
    lines.append("### 正例（建议合并方向）")
    lines.append("")
    lines.append(
        "- ✅ 「汽车整车」 ← 「汽车」「电动乘用车」「综合乘用车」"
        "「商用载客车」「商用载货车」"
    )
    lines.append(
        "- ✅ 「汽车零部件」 ← 「其他汽车零部件」「汽车电子电气系统」"
        "「车身附件及饰件」「胎压监测」"
    )
    lines.append(
        "- ✅ 「摩托车」 ← 「摩托车及其他」「两轮车」"
    )
    lines.append(
        "- ✅ 「家电零部件」 ← 「家电零部件Ⅱ」「家电零部件Ⅲ」"
    )
    lines.append("")

    lines.append("### 输出格式（严格紧凑 JSON，禁止 markdown 代码块或前后说明）")
    lines.append("")
    lines.append("```json")
    lines.append('{"version":"merge-v2","merges":[')
    lines.append(
        '{"target":"汽车整车","sources":["汽车","电动乘用车","综合乘用车","商用载客车","商用载货车"],"rationale":"乘用车/商用车/汽车整车均为整车制造主题"},'
    )
    lines.append(
        '{"target":"摩托车","sources":["摩托车及其他","两轮车"],"rationale":"摩托车/两轮电动车同主题"},'
    )
    lines.append("...")
    lines.append("]}")
    lines.append("```")
    lines.append("")
    lines.append(
        "字段说明：`target` ≤12 字符；`sources` ≥1 项且不含 target 自己；"
        "`rationale` 5~40 字必填。"
    )
    lines.append("")

    lines.append(
        f"### 输入数据：dim_sector_group 现状（{len(groups)} 组，"
        "按成员数倒序）"
    )
    lines.append("")
    lines.append(
        "格式：`### <name>  (×N)  id=<id>` 后接 rationale 与成员列表（"
        "ts_code · name · idx_type · src）。"
    )
    lines.append(
        "**判断合并时务必看成员明细**——很多组名只是同名，"
        "成员才能体现真正主题。"
    )
    lines.append("")

    for g in groups:
        lines.append(
            f"#### {g['name']}  (×{g['members_count']})  id={g['id']}"
        )
        if g["notes"]:
            lines.append(f"- ratio: {g['notes']}")
        for m in g["members"]:
            lines.append(
                f"  - `{m['ts_code']}` {m['name']} | "
                f"{m['idx_type']} | {m['src']}"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "**重要提示**：合并 JSON 必须包含 `version` 和 `merges` 两个字段，"
        "merges 是数组。AI 输出后请只保留 `{...}` 这一段 JSON 文本，"
        "保存到 `.huiye/_sector_merge_output.json` 后即可应用。"
    )
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    out_path.write_text(content, encoding="utf-8")

    total_members = sum(g["members_count"] for g in groups)
    return {
        "groups": len(groups),
        "members": total_members,
        "char_count": len(content),
        "tokens_estimate": len(content) // 3,
    }


def reset_all_clustering() -> Dict[str, int]:
    """清空整张聚类表 —— 删除 dim_sector_group + UPDATE dim_sector.group_id=NULL。

    用途：
      - LLM 聚类失败 / 偷懒（如 AIGC 组吞 500 个不相关板块）后回滚
      - 主人想完全重跑（换更细的 prompt 后从零再来）

    Returns:
        {"sectors_reset": <被取消 group_id 的板块数>,
         "groups_deleted": <被删的 dim_sector_group 行数>}
    """
    db = get_market_db()
    with db.connect() as conn:
        cur1 = conn.execute(
            "UPDATE dim_sector SET group_id = NULL WHERE group_id IS NOT NULL"
        )
        sectors_reset = cur1.rowcount
        cur2 = conn.execute("DELETE FROM dim_sector_group")
        groups_deleted = cur2.rowcount
    return {
        "sectors_reset": int(sectors_reset),
        "groups_deleted": int(groups_deleted),
    }


def get_clustering_stats() -> Dict[str, Any]:
    """返回当前聚类状态快照（CLI / GUI 显示用）。"""
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM dim_sector WHERE idx_type IS NOT NULL"
        ).fetchone()[0]
        clustered = conn.execute(
            "SELECT COUNT(*) FROM dim_sector "
            "WHERE group_id IS NOT NULL AND idx_type IS NOT NULL"
        ).fetchone()[0]
        groups = conn.execute(
            "SELECT COUNT(*) FROM dim_sector_group"
        ).fetchone()[0]
        biggest = conn.execute(
            "SELECT g.name, g.members_count "
            "FROM dim_sector_group g "
            "ORDER BY g.members_count DESC LIMIT 5"
        ).fetchall()
    return {
        "total_sectors": int(total),
        "clustered_sectors": int(clustered),
        "unclustered_sectors": int(total) - int(clustered),
        "groups_total": int(groups),
        "biggest_groups": [dict(r) for r in biggest],
    }


# ---------------------------------------------------------------------------
# 手动微调（自动入库后主人改正用）
# ---------------------------------------------------------------------------


def merge_groups(target_name: str, source_names: List[str]) -> int:
    """把 source_names 里的组合并到 target_name；source 行被删。

    Returns: 转移的成员数。
    """
    db = get_market_db()
    moved = 0
    with db.connect() as conn:
        target = conn.execute(
            "SELECT id FROM dim_sector_group WHERE name = ?",
            (target_name,),
        ).fetchone()
        if not target:
            raise ValueError(f"目标组 {target_name!r} 不存在")
        gid = int(target["id"])
        for src in source_names:
            row = conn.execute(
                "SELECT id FROM dim_sector_group WHERE name = ?",
                (src,),
            ).fetchone()
            if not row:
                continue
            sid = int(row["id"])
            cur = conn.execute(
                "UPDATE dim_sector SET group_id = ? WHERE group_id = ?",
                (gid, sid),
            )
            moved += cur.rowcount
            conn.execute(
                "DELETE FROM dim_sector_group WHERE id = ?", (sid,),
            )
        # 刷新 members_count
        conn.execute(
            "UPDATE dim_sector_group SET members_count = "
            " (SELECT COUNT(*) FROM dim_sector WHERE group_id = ?) "
            "WHERE id = ?",
            (gid, gid),
        )
    return moved


def split_member(
    ts_code: str, new_group_name: Optional[str] = None,
) -> int:
    """把单个板块从原组拆出来，独立成组（新组 name 默认用板块 name）。

    Returns: 新组 id。
    """
    db = get_market_db()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name, group_id FROM dim_sector WHERE ts_code = ?",
            (ts_code,),
        ).fetchone()
        if not row:
            raise ValueError(f"ts_code {ts_code!r} 不存在")
        old_gid = row["group_id"]
        new_name = new_group_name or str(row["name"])

        existing = conn.execute(
            "SELECT id FROM dim_sector_group WHERE name = ?", (new_name,),
        ).fetchone()
        if existing:
            new_gid = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO dim_sector_group "
                "(name, members_count, created_by, notes) "
                "VALUES (?, 1, 'manual', '从原组拆出')",
                (new_name,),
            )
            new_gid = int(cur.lastrowid)
        conn.execute(
            "UPDATE dim_sector SET group_id = ? WHERE ts_code = ?",
            (new_gid, ts_code),
        )
        # 刷新两组 members_count
        if old_gid is not None:
            conn.execute(
                "UPDATE dim_sector_group SET members_count = "
                " (SELECT COUNT(*) FROM dim_sector WHERE group_id = ?) "
                "WHERE id = ?",
                (old_gid, old_gid),
            )
        conn.execute(
            "UPDATE dim_sector_group SET members_count = "
            " (SELECT COUNT(*) FROM dim_sector WHERE group_id = ?) "
            "WHERE id = ?",
            (new_gid, new_gid),
        )
    return new_gid


# ---------------------------------------------------------------------------
# 一键聚类：直调 DeepSeek V4 Pro（1M 上下文，1510 个板块一次塞下）
# ---------------------------------------------------------------------------


_LLM_SYSTEM_PROMPT = """你是 A 股投资题材聚类专员，工作风格严谨细致、绝不糊弄。
你的任务：把输入的板块指数清单按【投资主题】聚类。

**核心要求（不可妥协）**：
- 每个 ts_code 必须出现且只出现一次（不能漏、不能重、不能编造）
- 每组必须给出 rationale（分组原因），强迫你逐组思考，不许把无关板块塞同一组
- 单组成员宁可拆细也不错合并：与其把"机器人/AI/芯片"塞 AIGC 大筐，不如让它们各自独立
- 单成员组完全允许（地域板块基本都该独立）"""


_LLM_USER_PROMPT_TPL = """以下是 A 股各类板块指数清单（共 {total} 个），来源包含
东方财富 dc（concept 概念 / industry 行业 / region 地域）和同花顺 ths
（industry 行业 / concept 概念）。请按【投资主题】聚类。

### 聚类原则（严格遵守，违反任意一条本次输出作废）

1. **同名跨源必同组**：「AIGC概念」(dc) + 「AIGC概念」(ths) → 同组，组名 "AIGC"
2. **同根细分必同组**：白酒/白酒Ⅱ/白酒Ⅲ/酿酒概念 → 组名 "白酒"
3. **紧密相关概念才合并**：仅当语义高度重合才合并，如 ChatGPT概念/大模型/AIGC概念 → "AIGC"
4. **跨产业绝不硬合并**：以下都是【独立主题】，不许塞一起：
   - AIGC ≠ 机器人 ≠ 智能传感器 ≠ 芯片设计 ≠ 5G ≠ 工业互联网
   - 锂电池 ≠ 氢能源 ≠ 储能 ≠ 光伏 ≠ 风电
   - 半导体 ≠ 消费电子 ≠ PCB ≠ 面板
5. **后缀无意义**：含 "指数 / Ⅱ / Ⅲ / Ⅳ / 概念 / 板块 / 含一字" 的等价于前缀
6. **单组上限 ≤ 15 个**（除下方"超大主题白名单"外），超过就拆细
7. **超大主题白名单（这几个组可以 >15 个，其他绝对不行）**：
   AIGC / 半导体 / 锂电池 / 光伏 / 新能源车 / 房地产 / 银行 / 医药商业 / 软件开发
8. **每个 ts_code 必须恰好出现一次**（必现+不重+不造）
9. **单成员组允许**且鼓励——地域板块（"北京板块/上海板块"）基本各自独立
10. **目标**：1510 → 700~900 组（不强求，但若 < 400 组说明你又在偷懒塞大筐）

### 反例（绝对禁止）

❌ 把机器人、传感器、ChatGPT、芯片设计、6G 全塞到 AIGC 组里
❌ 把白酒、啤酒、葡萄酒、乳业 全塞到 "饮料" 组里（白酒和乳业是不同主题）
❌ 输出几个真组之后用一个超大组吞掉剩余所有板块
❌ 编造不在输入清单里的 ts_code（必须从输入 sectors 里复制）

### 正例

✅ 「AIGC」组：仅含 AIGC概念/AI应用/AI生成内容/大模型/ChatGPT概念（5~10 个，纯内容生成赛道）
✅ 「机器人」组：仅含机器人概念/工业机器人/服务机器人/人形机器人（4~6 个）
✅ 「白酒」组：白酒/白酒Ⅱ/白酒概念/酿酒概念（3~5 个）
✅ 「上海板块」组：单成员

### 输出格式（严格紧凑 JSON，绝对禁止 markdown 代码块或前后说明文字）

{{"version":"llm-v2","groups":[
{{"name":"白酒","rationale":"DC 概念+THS 行业+THS 概念三源酒类同主题","ts_codes":["BK0896.DC","881273.TI","885525.TI"]}},
{{"name":"机器人","rationale":"机器人产业链通用板块汇总（不含传感器单独主题）","ts_codes":["BKxxxx.DC","..."]}},
...
]}}

字段说明：
- name：1~12 字符，不带 "概念/板块/指数" 后缀
- rationale：5~40 字，说明为什么这几个 ts_code 同组、为何排除某类（**强制必填**）
- ts_codes：必须从下方输入清单原样复制，不许编造或截短

### 输入清单（{total} 行，格式: ts_code<TAB>name<TAB>idx_type<TAB>src）

{sectors_block}
"""


#: DeepSeek V4 Pro 的最大输出上限（主人确认 = 384K tokens，2026-05-28 12:51）
#: 项目其他模块 DEFAULT_MAX_OUTPUT_TOKENS=65536 是 V4 通用保守值，本聚类
#: 任务单点拉满，避免 1510 板块紧凑 JSON 输出在 32~64K token 处被截断
_DEEPSEEK_V4_PRO_MAX_OUTPUT = 384 * 1024


def _approximate_max_tokens(n_sectors: int) -> int:
    """直接给 DeepSeek V4 Pro 输出上限 384K tokens。

    1510 板块紧凑 JSON 实测约 30K~50K token；之前 32K 截断在 79K 字符。
    拉满 384K 留 8x 余量，从根本上避开截断问题。
    """
    return _DEEPSEEK_V4_PRO_MAX_OUTPUT


def _call_deepseek_json(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    progress: Optional[Any] = None,
    label: str = "LLM",
) -> Dict[str, Any]:
    """通用 DeepSeek V4 Pro 调用：JSON object 返回 + thinking 关闭 + 流式进度。

    抽公因子用，cluster / merge_pass 两个二阶段都可复用。
    """
    from core.ai_config import AIConfig
    from openai import OpenAI

    cfg = AIConfig()
    pcfg = cfg.get_provider_config("deepseek")
    api_key = pcfg.get("api_key")
    base_url = pcfg.get("base_url") or "https://api.deepseek.com/v1"
    if not api_key:
        raise RuntimeError(
            "DeepSeek api_key 未配置（检查 ai_config.json / 环境变量 DEEPSEEK_API_KEY）"
        )

    client = OpenAI(api_key=api_key, base_url=base_url)
    model = pcfg.get("model") or "deepseek-v4-pro"

    if progress:
        progress(f"[{label}] 调 {model}, max_tokens={max_tokens}")

    create_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": True,
        "response_format": {"type": "json_object"},
    }
    if model.startswith("deepseek-v4"):
        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    parts: List[str] = []
    try:
        response = client.chat.completions.create(**create_kwargs)
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                parts.append(delta.content)
                if progress and len(parts) % 200 == 0:
                    progress(f"  ...已收到 {sum(len(p) for p in parts)} 字符")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DeepSeek 调用失败: {exc}") from exc

    raw = "".join(parts).strip()
    if not raw:
        raise RuntimeError("DeepSeek 返回空内容")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                raise RuntimeError(f"DeepSeek JSON 解析失败: {exc}") from exc
        raise RuntimeError(f"DeepSeek JSON 解析失败: {exc}") from exc


def cluster_via_deepseek(
    *,
    output_path: Path,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """直调 DeepSeek V4 Pro 跑一次性聚类，落 JSON 到 output_path。

    Returns:
        DeepSeek 解析后的 payload dict（与 _sector_clustering_output.json 一致）

    Raises:
        RuntimeError: API 失败 / JSON 解析失败 / 输出 ts_code 覆盖不全
    """
    sectors = load_unclustered_sectors()
    if not sectors:
        raise RuntimeError("没有未聚类板块（dim_sector.group_id 已全填）")

    sectors_block = "\n".join(
        f"{s.ts_code}\t{s.name}\t{s.idx_type}\t{s.src}"
        for s in sectors
    )
    user_prompt = _LLM_USER_PROMPT_TPL.format(
        total=len(sectors),
        sectors_block=sectors_block,
    )

    if progress:
        progress(f"[LLM] 调 deepseek-v4-pro 聚类 {len(sectors)} 个板块...")
    max_tok = _approximate_max_tokens(len(sectors))
    if progress:
        progress(f"[LLM] max_tokens={max_tok} (估算)")

    payload = _call_deepseek_json(
        system_prompt=_LLM_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=max_tok,
        progress=progress,
        label="LLM",
    )

    # 总是先把原始结果写盘，方便 debug，但闸门不通过仍 raise
    raw_dump_path = output_path.with_suffix(".raw.json")
    raw_dump_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dump_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    quality = _assess_quality(payload, sectors)
    if progress:
        progress(
            f"[LLM] 收到 {quality['groups_count']} 组 / "
            f"覆盖 {quality['covered']}/{quality['expected']} 板块"
            f" / 单组最大 {quality['max_group_size']}"
            f" / 缺 rationale {quality['missing_rationale']}"
        )
        for warn in quality["warnings"]:
            progress(f"  ⚠ {warn}")
        for fatal in quality["fatals"]:
            progress(f"  ✖ {fatal}")

    if quality["fatals"]:
        raise RuntimeError(
            f"LLM 输出未过质量闸门（已存原始结果到 {raw_dump_path}），"
            "失败原因：\n  - " + "\n  - ".join(quality["fatals"])
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


# ---------------------------------------------------------------------------
# 二阶段：merge-pass —— 拿现有 group 名再调 LLM 推荐合并
# ---------------------------------------------------------------------------

_MERGE_SYSTEM_PROMPT = """你是 A 股投资题材近义合并专员。
输入是 dim_sector_group 表里所有现有组名（已经过一阶段保守拆分），
你的任务：找出语义近义但被分开的组，建议把它们合并到一起。"""


_MERGE_USER_PROMPT_TPL = """以下是 {total} 个 dim_sector_group 现有组名（一阶段保守拆分结果，每个组都是已经合并过 DC+THS 同名的）。
请逐项判断：哪些组应该再合并到一起，因为它们其实是同一投资主题。

### 合并原则

1. **同根细分必合并**：白酒/白酒Ⅱ/白酒Ⅲ/酿酒/酿酒概念 → target="白酒"
2. **跨概念近义合并**：AIGC/AI应用/ChatGPT概念/大模型/AI生成内容 → target="AIGC"
3. **行业-概念语义重合可合并**：「锂电池（DC行业）」+ 「锂电池」概念组 → target="锂电池"
4. **跨产业绝不合并**：锂电池 ≠ 氢能源 ≠ 储能；半导体 ≠ 5G ≠ 通信
5. **target 必须是 sources 中的 1 个或者另起一个简洁标准名**
6. **每个组名只能出现在一处合并里**（一个 source 不能被合并到多个 target）
7. **没把握就不合并**——宁可放过一组近义也别错并语义不同的

### 输出格式（严格紧凑 JSON）

{{"version":"merge-v1","merges":[
{{"target":"白酒","sources":["白酒（DC）","白酒（THS）","酿酒"],"rationale":"白酒系细分主题"}},
{{"target":"AIGC","sources":["AI应用","ChatGPT概念","大模型"],"rationale":"AI 内容生成核心赛道"}},
...
]}}

字段说明：
- target: 合并后的目标组名（1~12 字符，不带"概念/板块/指数"后缀）
- sources: 被合并的现有组名（≥1 个，且不能含 target 自己）
- rationale: 5~30 字说明为什么合并

### 不需要合并的组

如果某个组本身已经是独立主题（如「上海板块」「6G概念」），不要出现在任何 merges[] 里就好。

### 现有组名清单（{total} 个，格式: id<TAB>name<TAB>members_count）

{groups_block}
"""


def build_merge_prompt_for_external() -> Dict[str, Any]:
    """生成二阶段合并的完整 prompt + 原始数据，供主人喂给任意外部 LLM。

    返回：
      {
        "system": <system prompt 文本>,
        "user": <user prompt 文本，含全部 group 列表>,
        "total_groups": <int>,
        "expected_schema": <期望输出 JSON schema 的简短描述>
      }

    主人可拷贝到 Claude / GPT / Gemini / Kimi 等任意 LLM 里跑，
    输出结果保存为 .huiye/_sector_merge_output.json 后用
    `python tools/cluster_sectors.py merge-pass --no-llm --apply` 落库。
    """
    groups = load_current_groups()
    if not groups:
        raise RuntimeError("dim_sector_group 为空，请先跑一阶段聚类")

    groups_block = "\n".join(
        f"{g['id']}\t{g['name']}\t{g['members_count']}"
        for g in groups
    )
    user_prompt = _MERGE_USER_PROMPT_TPL.format(
        total=len(groups),
        groups_block=groups_block,
    )
    return {
        "system": _MERGE_SYSTEM_PROMPT,
        "user": user_prompt,
        "total_groups": len(groups),
        "expected_schema": (
            '{"version":"merge-v1","merges":[{"target":"...",'
            '"sources":["..."],"rationale":"..."}, ...]}'
        ),
    }


def dump_merge_prompt_to_markdown(out_path: Path) -> Dict[str, int]:
    """把 build_merge_prompt_for_external 的内容写成 md 文档（主人复制用）。

    Returns:
        {"groups": <int>, "system_chars": <int>, "user_chars": <int>}
    """
    bundle = build_merge_prompt_for_external()
    sys_text = bundle["system"]
    user_text = bundle["user"]
    total = bundle["total_groups"]

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = []
    lines.append("# 板块二阶段合并·外部 AI 投喂包")
    lines.append("")
    lines.append(f"> 生成时间：{now}  ")
    lines.append(f"> 当前组数：**{total}**  ")
    lines.append(
        f"> System 长度：{len(sys_text):,} 字符 · "
        f"User 长度：{len(user_text):,} 字符 · "
        f"合计 ≈ {len(sys_text) + len(user_text):,} 字符 "
        f"(粗估 token = 字符数 / 1.5 ≈ "
        f"{(len(sys_text) + len(user_text)) // 1500}K)"
    )
    lines.append("")

    lines.append("## 用法")
    lines.append("")
    lines.append("1. 把【二、System Prompt】整段贴到 LLM 的 system / instruction 字段")
    lines.append("2. 把【三、User Prompt】整段贴到 LLM 的 user / message 字段")
    lines.append("3. 设置 LLM 输出参数：")
    lines.append("   - `temperature ≤ 0.2`（聚类是确定性任务）")
    lines.append("   - `response_format = json_object` 或类似 JSON 强制")
    lines.append("   - `max_tokens` 给足（建议 ≥ 64K，本任务输出 ~30K token）")
    lines.append("4. 拿到 JSON 输出（结构见【四、期望输出 schema】），保存为：")
    lines.append("   ```")
    lines.append("   .huiye/_sector_merge_output.json")
    lines.append("   ```")
    lines.append("5. 落库：")
    lines.append("   ```bash")
    lines.append("   python tools/cluster_sectors.py merge-pass --no-llm --dry-run  # 先校验")
    lines.append("   python tools/cluster_sectors.py merge-pass --no-llm --apply    # 真合并")
    lines.append("   ```")
    lines.append("")

    lines.append("## 二、System Prompt（复制下方代码块整段）")
    lines.append("")
    lines.append("```")
    lines.append(sys_text)
    lines.append("```")
    lines.append("")

    lines.append("## 三、User Prompt（复制下方代码块整段，含全部组列表）")
    lines.append("")
    lines.append("```")
    lines.append(user_text)
    lines.append("```")
    lines.append("")

    lines.append("## 四、期望输出 schema")
    lines.append("")
    lines.append("LLM 必须输出严格 JSON，结构如下：")
    lines.append("")
    lines.append("```json")
    lines.append("{")
    lines.append('  "version": "merge-v1",')
    lines.append('  "merges": [')
    lines.append('    {')
    lines.append('      "target": "白酒",')
    lines.append('      "sources": ["白酒Ⅱ", "白酒Ⅲ", "酿酒概念"],')
    lines.append('      "rationale": "白酒系细分主题"')
    lines.append('    },')
    lines.append('    {')
    lines.append('      "target": "AIGC",')
    lines.append('      "sources": ["AI应用", "ChatGPT概念", "Sora概念"],')
    lines.append('      "rationale": "AI 内容生成核心赛道"')
    lines.append('    }')
    lines.append('  ]')
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("- `target`: 合并后的目标组名（必须是简洁标准名，1~12 字符）")
    lines.append("- `sources`: 被合并到 target 的现有组名列表（必须从【三】里复制原文）")
    lines.append("- `rationale`: 5~30 字说明为什么合并（强制必填）")
    lines.append("")
    lines.append("## 五、落库系统的自动质量闸门（参考）")
    lines.append("")
    lines.append("`apply_merges` 在落库前会自动：")
    lines.append("- 过滤 `sources` 里不在字典的（LLM 编造名）")
    lines.append("- 过滤 `sources` 里等于 target 自身的（schema 误解）")
    lines.append("- 拒绝 `合并后剩 < 40%` 的偷懒输出")
    lines.append("- 拒绝单 target 吃 > 12 个 sources 的乱合并")
    lines.append("- 同名 source 被多 target 抢吃时只认第一个")
    lines.append("")
    lines.append("所以主人**不必担心 LLM 偶有错误**，落库时自动排雷。")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "groups": total,
        "system_chars": len(sys_text),
        "user_chars": len(user_text),
    }


def load_current_groups() -> List[Dict[str, Any]]:
    """读 dim_sector_group 全部行，给二阶段 LLM 推荐合并用。"""
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, name, members_count, notes "
            "FROM dim_sector_group ORDER BY name"
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "members_count": int(r["members_count"]),
            "notes": str(r["notes"] or ""),
        }
        for r in rows
    ]


def propose_merges_via_deepseek(
    *,
    output_path: Path,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """二阶段：拿现有 dim_sector_group 名喂 LLM 推荐合并，落 JSON。

    与一阶段的区别：
      - 输入小（1229 行 ≈ 30K token），LLM 不会偷懒
      - 不涉及 ts_code，只看 name → 不会编造 code
      - rationale 仍强制必填

    Returns:
        {"version": "merge-v1", "merges": [{"target", "sources", "rationale"}]}

    Raises:
        RuntimeError: API 失败 / JSON 解析失败 / 校验未过
    """
    groups = load_current_groups()
    if len(groups) < 2:
        raise RuntimeError(f"组数太少（{len(groups)}），无须合并")

    groups_block = "\n".join(
        f"{g['id']}\t{g['name']}\t{g['members_count']}"
        for g in groups
    )
    user_prompt = _MERGE_USER_PROMPT_TPL.format(
        total=len(groups),
        groups_block=groups_block,
    )

    # 输入 ≈ 50K token；输出 merges 列表也很小（最多几百条），32K 输出绰绰有余
    payload = _call_deepseek_json(
        system_prompt=_MERGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=64 * 1024,
        progress=progress,
        label="MERGE",
    )

    raw_dump_path = output_path.with_suffix(".raw.json")
    raw_dump_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dump_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    quality = _assess_merge_quality(payload, groups)
    if progress:
        progress(
            f"[MERGE] 收到 {quality['merges_count']} 条合并 / "
            f"涉及 {quality['affected_groups']} 个原组 / "
            f"将剩 {quality['final_groups']} 组"
        )
        for warn in quality["warnings"]:
            progress(f"  ⚠ {warn}")
        for fatal in quality["fatals"]:
            progress(f"  ✖ {fatal}")

    if quality["fatals"]:
        raise RuntimeError(
            f"merge 输出未过质量闸门（原始结果在 {raw_dump_path}），"
            "失败原因：\n  - " + "\n  - ".join(quality["fatals"])
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


# 二阶段闸门阈值（比一阶段更宽松，因为输入小、风险低）
_MERGE_MAX_SOURCES_PER_TARGET = 12      # 单 target 最多吃 12 个 sources
_MERGE_AFFECTED_RATIO_FATAL = 0.6        # 合并后剩 < 40% 视为 LLM 把所有都合一起的偷懒症


def _assess_merge_quality(
    payload: Dict[str, Any],
    groups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """诊断 merge-pass 输出质量。"""
    name_to_id = {g["name"]: g["id"] for g in groups}
    valid_names = set(name_to_id.keys())

    fatals: List[str] = []
    warnings: List[str] = []
    seen_sources: Dict[str, str] = {}    # source name → 第一次的 target

    merges = payload.get("merges") if isinstance(payload, dict) else None
    if not isinstance(merges, list):
        return {
            "merges_count": 0, "affected_groups": 0, "final_groups": len(groups),
            "warnings": [], "fatals": ["payload.merges 不是 list"],
        }

    affected = 0
    big_offenders: List[Tuple[str, int]] = []
    missing_rationale = 0
    for i, m in enumerate(merges):
        if not isinstance(m, dict):
            warnings.append(f"merges[{i}] 不是 dict，跳过")
            continue
        target = str(m.get("target", "")).strip()
        sources_raw = m.get("sources") or []
        if not target:
            warnings.append(f"merges[{i}] target 为空")
            continue
        if not isinstance(sources_raw, list) or not sources_raw:
            warnings.append(f"merges[{i}] ({target!r}) sources 为空")
            continue

        rationale = m.get("rationale") or m.get("notes") or ""
        if not str(rationale).strip():
            missing_rationale += 1

        n_sources = len(sources_raw)
        if n_sources > _MERGE_MAX_SOURCES_PER_TARGET:
            big_offenders.append((target, n_sources))

        # source 含自身 是 LLM 的 schema 通用误解（无害，apply 时自动过滤），不报 warning
        for src in sources_raw:
            s = str(src).strip()
            if not s or s == target:
                continue
            if s not in valid_names:
                warnings.append(
                    f"merges[{i}] ({target!r}) source 不在字典: {s!r}"
                )
                continue
            if s in seen_sources:
                warnings.append(
                    f"source 重复: {s!r} 同时被 {seen_sources[s]!r} 和 {target!r} 吃"
                )
                continue
            seen_sources[s] = target
            affected += 1

    if big_offenders:
        big_offenders.sort(key=lambda x: -x[1])
        worst = big_offenders[:5]
        fatals.append(
            f"单 target 吃 sources 过多（>{_MERGE_MAX_SOURCES_PER_TARGET}）："
            + ", ".join(f"{n}×{s}" for n, s in worst)
        )

    final_groups = len(groups) - affected + sum(
        1 for m in merges
        if isinstance(m, dict)
        and str(m.get("target", "")).strip() not in valid_names
    )
    if affected and (final_groups / len(groups)) < (1 - _MERGE_AFFECTED_RATIO_FATAL):
        fatals.append(
            f"合并后剩 {final_groups}/{len(groups)} 组 "
            f"({final_groups / len(groups):.0%}) "
            f"< {(1 - _MERGE_AFFECTED_RATIO_FATAL):.0%} 阈值，"
            "可能 LLM 在偷懒乱合并"
        )

    if missing_rationale > len(merges) * 0.2 and merges:
        warnings.append(
            f"{missing_rationale}/{len(merges)} 条缺 rationale，"
            "可能 LLM 没按 v1 schema 输出"
        )

    return {
        "merges_count": len(merges),
        "affected_groups": affected,
        "final_groups": max(final_groups, 0),
        "warnings": warnings,
        "fatals": fatals,
    }


def apply_merges(
    payload: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """把 propose_merges_via_deepseek 的输出落库（循环调 merge_groups）。

    Returns:
        {
          "merges_applied", "groups_removed", "members_moved",
          "skipped": [...原因...]
        }
    """
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        valid_names = {
            str(r["name"]) for r in conn.execute(
                "SELECT name FROM dim_sector_group"
            ).fetchall()
        }

    merges = payload.get("merges") or []
    applied = 0
    members_moved = 0
    groups_removed = 0
    skipped: List[str] = []

    for i, m in enumerate(merges):
        if not isinstance(m, dict):
            skipped.append(f"merges[{i}] 不是 dict")
            continue
        target = str(m.get("target", "")).strip()
        sources_raw = m.get("sources") or []
        if not target or not sources_raw:
            skipped.append(f"merges[{i}] target/sources 为空")
            continue

        # source 过滤：去掉无效 / 自身 / 不存在的
        sources = [
            s for s in (str(x).strip() for x in sources_raw)
            if s and s != target and s in valid_names
        ]
        if not sources:
            skipped.append(f"merges[{i}] ({target!r}) 无可合并 source")
            continue

        # target 不存在场景：把第一个 source 改名为 target，再吃剩下
        rename_base: Optional[str] = None
        if target not in valid_names:
            rename_base = sources[0]
            sources = sources[1:]

        if dry_run:
            applied += 1
            groups_removed += len(sources) + (1 if rename_base else 0)
            continue

        if rename_base:
            db_conn = get_market_db()
            with db_conn.connect() as conn:
                conn.execute(
                    "UPDATE dim_sector_group SET name = ? WHERE name = ?",
                    (target, rename_base),
                )
            valid_names.discard(rename_base)
            valid_names.add(target)
            applied += 1
            if not sources:
                continue

        try:
            moved = merge_groups(target, sources)
            if not rename_base:
                applied += 1
            members_moved += moved
            groups_removed += len(sources)
            for s in sources:
                valid_names.discard(s)
        except ValueError as exc:
            skipped.append(f"merges[{i}] ({target!r}) 失败: {exc}")

    return {
        "merges_applied": applied,
        "groups_removed": groups_removed,
        "members_moved": members_moved,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# 质量闸门：阻止 LLM 偷懒结果直接落库
# ---------------------------------------------------------------------------

#: 允许 >15 个成员的"超大主题"白名单（覆盖宽广产业链的概念）
_BIG_GROUP_WHITELIST = {
    "AIGC", "半导体", "锂电池", "光伏", "新能源车", "新能源汽车",
    "房地产", "银行", "医药商业", "软件开发", "化工原料", "工程机械",
    "电力", "煤炭", "白色家电",
}

#: 单组上限（白名单外）—— 超过即视为 LLM 把垃圾筐塞了
_DEFAULT_GROUP_MAX = 15
#: 漏分率阈值（缺多少视为 fatal）
_MISSING_RATIO_FATAL = 0.05  # > 5% 直接 fail
#: 编造 ts_code 比例阈值
_FABRICATE_RATIO_FATAL = 0.005  # > 0.5%


def _assess_quality(
    payload: Dict[str, Any],
    sectors: List[SectorRecord],
) -> Dict[str, Any]:
    """诊断 LLM 输出质量。返回:

    {
      "groups_count", "expected", "covered", "max_group_size",
      "missing_rationale", "warnings": [...], "fatals": [...]
    }
    """
    expected = {s.ts_code for s in sectors}
    valid = set(expected)

    groups = payload.get("groups", []) if isinstance(payload, dict) else []
    got: set = set()
    fabricated: List[str] = []
    big_offenders: List[Tuple[str, int]] = []
    missing_rationale = 0
    seen_codes: Dict[str, str] = {}     # ts_code → 第一次出现的组名
    duplicate_assignments: List[str] = []

    max_size = 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name", "")).strip()
        codes = g.get("ts_codes") or []
        if not isinstance(codes, list):
            continue
        rationale = g.get("rationale") or g.get("notes")
        if not rationale or not str(rationale).strip():
            missing_rationale += 1

        size = len(codes)
        max_size = max(max_size, size)
        if size > _DEFAULT_GROUP_MAX and name not in _BIG_GROUP_WHITELIST:
            big_offenders.append((name, size))

        for c in codes:
            cs = str(c)
            if cs in seen_codes and seen_codes[cs] != name:
                duplicate_assignments.append(
                    f"{cs} 同时被分到 {seen_codes[cs]!r} 和 {name!r}"
                )
            else:
                seen_codes[cs] = name
            if cs not in valid:
                fabricated.append(cs)
            got.add(cs)

    missing = expected - got
    warnings: List[str] = []
    fatals: List[str] = []

    miss_ratio = len(missing) / len(expected) if expected else 0
    if miss_ratio > _MISSING_RATIO_FATAL:
        fatals.append(
            f"漏分 {len(missing)}/{len(expected)} ({miss_ratio:.1%}) > "
            f"{_MISSING_RATIO_FATAL:.0%} 阈值"
        )
    elif missing:
        warnings.append(
            f"漏分 {len(missing)} 个（前 5: {list(missing)[:5]}），"
            "落库时这些将保持 group_id=NULL"
        )

    fab_ratio = len(fabricated) / max(len(got), 1)
    if fab_ratio > _FABRICATE_RATIO_FATAL:
        fatals.append(
            f"编造 ts_code {len(fabricated)} 个 ({fab_ratio:.1%}) > "
            f"{_FABRICATE_RATIO_FATAL:.0%} 阈值，前 5: {fabricated[:5]}"
        )
    elif fabricated:
        warnings.append(
            f"编造 ts_code {len(fabricated)} 个（前 5: {fabricated[:5]}），"
            "落库时将忽略"
        )

    if big_offenders:
        big_offenders.sort(key=lambda x: -x[1])
        worst = big_offenders[:5]
        fatals.append(
            f"单组超过 {_DEFAULT_GROUP_MAX} 且不在白名单 {len(big_offenders)} 个："
            + ", ".join(f"{n}×{s}" for n, s in worst)
            + "（典型 LLM 偷懒症状）"
        )

    if duplicate_assignments:
        warnings.append(
            f"重复分配 {len(duplicate_assignments)} 个（前 3: "
            + "; ".join(duplicate_assignments[:3])
            + "），落库以最后一个为准"
        )

    if missing_rationale > len(groups) * 0.2 and groups:
        warnings.append(
            f"{missing_rationale}/{len(groups)} 组缺 rationale，"
            "可能是 LLM 没按 v2 schema 输出"
        )

    return {
        "groups_count": len(groups),
        "expected": len(expected),
        "covered": len(got & expected),
        "max_group_size": max_size,
        "missing_rationale": missing_rationale,
        "warnings": warnings,
        "fatals": fatals,
    }


__all__ = [
    "SectorRecord",
    "GroupingResult",
    "load_unclustered_sectors",
    "export_for_subagent",
    "apply_clustering",
    "cluster_via_deepseek",
    "reset_all_clustering",
    "get_clustering_stats",
    "load_current_groups",
    "propose_merges_via_deepseek",
    "apply_merges",
    "load_groups_with_members",
    "load_unclustered_members",
    "export_groups_to_markdown",
    "dump_merge_pass_prompt",
    "query_grouped_sectors_for_date",
    "query_grouped_sectors_with_members",
    "count_grouped_sectors_for_date",
    "merge_groups",
    "split_member",
]
