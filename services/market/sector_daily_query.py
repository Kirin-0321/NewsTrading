"""板块日行情历史查询（GUI 题材走势 Tab + 未来 Web API 用）。

业务定位
--------
与 :mod:`services.market.sector_daily_sync` 是一对：

* ``sector_daily_sync``  ：**写** —— 从 Tushare 拉数据落 ``fact_sector_daily``
* ``sector_daily_query`` ：**读** —— 按 ts_code 取一只板块的时间序列

为什么不放 ``gui/utils/market_db_helper.py``？
    因为 helper 里的查询是「按交易日查多板块」的横截面口径，本模块是
    「按板块查多交易日」的时间序列口径，未来 Web 化时会是独立的
    REST endpoint，单独成文件更清晰。

数据源
------
* ``market.fact_sector_daily``  字段 trade_date / ts_code / pct_chg /
  main_net_yi / main_elg_yi / main_lg_yi / limit_up_count /
  pct_chg_5d / rank_today
* ``market.dim_sector``          反查板块名

CLI
---
对称入口：``tools/sector_daily_query.py``
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from services.market.market_db import get_market_db

_log = logging.getLogger(__name__)


@dataclass
class SectorDailyRow:
    """单个交易日的板块行情快照。

    Fields:
        trade_date    YYYYMMDD
        pct_chg       当日涨跌幅 %（None 表示缺数据）
        main_net_yi   主力净流入 亿元
        main_elg_yi   超大单净流入 亿元
        main_lg_yi    大单净流入 亿元
        limit_up_count 板块内涨停股数
        pct_chg_5d    派生：近 5 日累计涨幅 %（一般只对 Top 10 板块算）
        rank_today    当日板块涨幅排名（1 = 涨幅第一）
    """

    trade_date: str
    pct_chg: Optional[float] = None
    main_net_yi: Optional[float] = None
    main_elg_yi: Optional[float] = None
    main_lg_yi: Optional[float] = None
    limit_up_count: Optional[int] = None
    pct_chg_5d: Optional[float] = None
    rank_today: Optional[int] = None


@dataclass
class SectorDailyHistory:
    """板块时间序列查询结果。

    Fields:
        ts_code   板块代码（如 ``BK0871.DC``）
        name      板块名称（dim_sector.name；字典都没匹配到则空串）
        idx_type  板块类型（"概念板块" / "行业板块"），从 dim_sector 反查
        in_dim    板块代码在 ``dim_sector`` 字典里是否存在
        rows      按 trade_date 升序排列的日行情列表
        count     rows 长度

    三种状态:
        * ``in_dim=False, count=0``：板块代码不存在，多半是 sector_ts_code
          写错了
        * ``in_dim=True,  count=0``：字典有名字（``name``/``idx_type`` 有值），
          但 ``fact_sector_daily`` 没数据——典型场景是"行业板块"，
          ``moneyflow_ind_dc`` 接口本就不返这类板块
        * ``in_dim=True,  count>0``：正常有数据
    """

    ts_code: str
    name: str
    idx_type: str = ""
    in_dim: bool = False
    rows: List[SectorDailyRow] = field(default_factory=list)
    count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转可序列化 dict（CLI / 未来 API 响应体用）。"""
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "idx_type": self.idx_type,
            "in_dim": self.in_dim,
            "count": self.count,
            "rows": [asdict(r) for r in self.rows],
        }


def get_sector_daily_history(
    ts_code: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    order: str = "asc",
) -> SectorDailyHistory:
    """按板块 ts_code 取该板块的逐日行情序列。

    Args:
        ts_code: 板块代码，例 ``BK0871.DC``。空串返回空结果（不抛错）。
        start_date: 起始交易日（含），``YYYYMMDD``；None = 不限。
        end_date: 终止交易日（含），``YYYYMMDD``；None = 不限。
        order: ``"asc"`` 按交易日升序（画图用，默认），``"desc"`` 降序
            （表格倒序展示）。

    Returns:
        :class:`SectorDailyHistory`，``rows`` 已按 ``order`` 排序。
        板块不存在 / market.db 损坏时返回空 ``rows`` 但不抛异常（log warning）。
        即使 ``rows`` 为空，也会**先反查 dim_sector** 拿到 name / idx_type，
        让上层能据此显示精准提示（区分"代码错"和"数据源未覆盖"）。

    SQL 性能:
        命中 ``idx_sector_code`` (ts_code, trade_date DESC) 索引，
        单板块全量约 500~1000 行，SSD 上 < 20ms。
        + 1 次 dim_sector 主键查询 < 1ms。
    """
    if not ts_code:
        return SectorDailyHistory(
            ts_code="", name="", idx_type="", in_dim=False,
            rows=[], count=0,
        )

    if order not in ("asc", "desc"):
        raise ValueError(f"order 只能是 'asc' 或 'desc'，得到 {order!r}")

    where_parts = ["s.ts_code = ?"]
    params: List[Any] = [ts_code]
    if start_date:
        where_parts.append("s.trade_date >= ?")
        params.append(start_date)
    if end_date:
        where_parts.append("s.trade_date <= ?")
        params.append(end_date)

    sql = (
        "SELECT s.trade_date, s.pct_chg, "
        "       s.main_net_yi, s.main_elg_yi, s.main_lg_yi, "
        "       s.limit_up_count, s.pct_chg_5d, s.rank_today "
        "FROM fact_sector_daily s "
        "WHERE " + " AND ".join(where_parts) + " "
        f"ORDER BY s.trade_date {'ASC' if order == 'asc' else 'DESC'}"
    )

    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            # 先反查 dim_sector，拿 name + idx_type（不依赖 fact 表有行）
            dim_row = conn.execute(
                "SELECT name, idx_type FROM dim_sector "
                "WHERE ts_code = ?",
                (ts_code,),
            ).fetchone()
            dim_name = str(dim_row["name"]) if dim_row else ""
            dim_idx_type = str(dim_row["idx_type"]) if dim_row else ""
            in_dim = dim_row is not None

            db_rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "get_sector_daily_history 失败 ts_code=%s: %s", ts_code, exc
        )
        return SectorDailyHistory(
            ts_code=ts_code, name="", idx_type="", in_dim=False,
            rows=[], count=0,
        )

    if not db_rows:
        return SectorDailyHistory(
            ts_code=ts_code,
            name=dim_name,
            idx_type=dim_idx_type,
            in_dim=in_dim,
            rows=[], count=0,
        )

    rows: List[SectorDailyRow] = []
    for r in db_rows:
        rows.append(SectorDailyRow(
            trade_date=str(r["trade_date"] or ""),
            pct_chg=_to_float(r["pct_chg"]),
            main_net_yi=_to_float(r["main_net_yi"]),
            main_elg_yi=_to_float(r["main_elg_yi"]),
            main_lg_yi=_to_float(r["main_lg_yi"]),
            limit_up_count=_to_int(r["limit_up_count"]),
            pct_chg_5d=_to_float(r["pct_chg_5d"]),
            rank_today=_to_int(r["rank_today"]),
        ))

    return SectorDailyHistory(
        ts_code=ts_code,
        name=dim_name,
        idx_type=dim_idx_type,
        in_dim=in_dim,
        rows=rows,
        count=len(rows),
    )


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_sector_names(ts_codes: List[str]) -> Dict[str, str]:
    """批量反查板块 ts_code → 中文名映射。

    业务定位：
        题材预测页 ``_render_theme_table`` 渲染前一次性取回当批板块名，
        把 ``BK0477.DC`` 之类的代码显示成 ``白酒 (BK0477.DC)``。

    Args:
        ts_codes: 板块代码列表，允许重复 / 含空串 / 含 None；
            内部会去重 + 过滤空值。

    Returns:
        ``{ts_code: name}``；查不到的键**不放进字典**（上层据此回退显示）。
        参数为空或 ``dim_sector`` 无任何命中时返回空 dict（不抛错）。

    SQL 性能:
        单次 ``WHERE ts_code IN (...)`` 命中主键索引，
        百级 ts_codes 单次查询 < 5ms。
    """
    cleaned = sorted({c for c in ts_codes if c})
    if not cleaned:
        return {}

    placeholders = ",".join("?" * len(cleaned))
    sql = (
        f"SELECT ts_code, name FROM dim_sector "
        f"WHERE ts_code IN ({placeholders})"
    )
    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            rows = conn.execute(sql, cleaned).fetchall()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "get_sector_names 失败 ts_codes=%s: %s", cleaned[:5], exc
        )
        return {}
    return {
        str(r["ts_code"]): str(r["name"])
        for r in rows
        if r["name"]
    }


__all__ = [
    "SectorDailyRow",
    "SectorDailyHistory",
    "get_sector_daily_history",
    "get_sector_names",
]
