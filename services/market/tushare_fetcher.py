"""Tushare 行情数据取数 + 入库（Phase M1b）。

接口调用清单（与 ``_tushare_field_audit.md §7.1`` 对齐）::

    1.  index_daily       × 7 大指数      → fact_index_daily
    2.  dc_index          (idx_type=概念) → dim_sector upsert
    3.  moneyflow_ind_dc  (concept)       → fact_sector_daily
    4.  limit_list_d      × 3 (U/Z/D)     → fact_limit_stock
    5.  kpl_list          (T 日)          → fact_limit_stock 合并连板信息
    6.  kpl_list          (T-1 日)        → 内存返回，供 metrics.calc_promotion_rate
    7.  moneyflow_hsgt    (T 日; 空兜底 T-1) → fact_hsgt_daily
    8.  top_list                          → fact_top_list（过滤可转债）
    9.  top_inst                          → fact_top_inst
    10. dc_daily          (top N 板块 5 天) → fact_sector_daily.pct_chg_5d 派生填充

合计约 16 次 API。CLS 两个接口（``cls_stock_shock`` / ``cls_market_shock``）
预留给 Phase M2 单独的 ``CLSEnricher`` 调用。

设计约束
--------
* **幂等**：所有 ingest 都用 ``INSERT OR REPLACE``，重跑同一天不会重复入库。
* **缓存**：默认检测 ``fact_index_daily`` 有当天数据则跳过 ingest，``force_refresh``
  开关一开就先 DELETE 当天全部 fact_* 再重拉。
* **金额单位**：所有 ``*_yi`` 后缀字段统一为「亿元」浮点；详见
  ``_tushare_field_audit.md §4 单位速查表``。
* **失败容忍**：单个接口异常不抛出，写入 ``warnings``，继续后续步骤。
  对端到端流程而言"丢一个 fact 表"比"全盘失败"友好得多。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.market.market_db import MarketDB
from services.market.tushare_client import TushareClient, TushareError

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 7 大指数 ts_code → 内部 key（与 MarketSummary.indices 字段名对齐）
INDEX_CODES: List[Tuple[str, str, str]] = [
    ("000001.SH", "sh", "上证"),
    ("399001.SZ", "sz", "深成指"),
    ("399006.SZ", "cyb", "创业板"),
    ("000300.SH", "hs300", "沪深300"),
    ("000688.SH", "kc50", "科创50"),
    ("000905.SH", "zz500", "中证500"),
    ("000852.SH", "zz1000", "中证1000"),
]

LIMIT_TYPES = ["U", "Z", "D"]

#: 同花顺 limit_list_ths 泳池中文名 → fact_limit_stock.limit_type 映射
#: 连扳池单独处理（仅 UPDATE tag，不入库），冲刺涨停去 fact_limit_sprint
THS_POOL_TO_LIMIT_TYPE: List[Tuple[str, str]] = [
    ("涨停池", "U"),
    ("炸板池", "Z"),
    ("跌停池", "D"),
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """``TushareMarketFetcher.fetch`` 的返回值。"""

    trade_date: str
    prev_trade_date: str
    api_call_count: int = 0
    elapsed_ms: int = 0

    #: 北向资金实际取到的日期（可能因 T 日空回退到 prev）
    hsgt_data_date: Optional[str] = None
    hsgt_is_delayed: bool = False

    #: 各 ingest 步骤写入的行数
    ingested: Dict[str, int] = field(default_factory=dict)

    #: 失败/降级的 warning
    warnings: List[str] = field(default_factory=list)

    #: 缓存命中跳过了哪些步骤
    skipped: List[str] = field(default_factory=list)

    #: kpl_list(prev_trade_date) 内存缓存，给 metrics.calc_promotion_rate 用
    kpl_prev_rows: List[Dict[str, Any]] = field(default_factory=list)

    #: daily 接口拉到的全市场涨跌家数（M6 新增）
    market_breadth: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class TushareMarketFetcher:
    """把指定交易日的所有 L1 行情数据取下并落到 ``market.db``。"""

    def __init__(
        self,
        client: TushareClient,
        db: MarketDB,
    ) -> None:
        self.client = client
        self.db = db

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def fetch(
        self,
        trade_date: str,
        prev_trade_date: str,
        *,
        top_sector_n: int = 10,
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> FetchResult:
        """把 ``trade_date`` 的全部行情数据拉到 ``market.db``。

        Args:
            trade_date: 目标交易日 ``YYYYMMDD``，由
                ``trade_date.resolve_trade_date`` 给出。
            prev_trade_date: 上一交易日 ``YYYYMMDD``，用于晋级率 + 北向兜底。
            top_sector_n: 仅对前 N 个板块拉 5 日历史算 ``pct_chg_5d``。
            force_refresh: True 时先清掉该日所有 fact 数据再重拉。
            progress_callback: 每个步骤前调用一次 ``cb(message)``，
                供 GUI Worker 实时更新进度文字。

        Returns:
            :class:`FetchResult`，含每步入库条数、警告、北向延迟标记等。
        """
        progress = progress_callback or (lambda _msg: None)
        t0 = time.time()
        api0 = self.client.call_count

        result = FetchResult(
            trade_date=trade_date, prev_trade_date=prev_trade_date
        )

        if force_refresh:
            progress("清理已有数据…")
            self._purge_trade_date(trade_date)
        elif self._has_index_data(trade_date):
            # 缓存命中：fact_index_daily 有当天数据，直接复用全部 fact_*
            _log.info(
                "fact_* 表已有 %s 数据，本次跳过 Tushare 拉取（force_refresh=False）",
                trade_date,
            )
            result.skipped.append("all")
            result.api_call_count = 0
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result

        steps: List[Tuple[str, Callable[[FetchResult], None]]] = [
            ("拉取 7 大指数", self._fetch_index_daily),
            ("拉取板块字典 (dc_index)", self._fetch_sector_dict),
            ("拉取板块资金流 (moneyflow_ind_dc)", self._fetch_sector_moneyflow),
            ("拉取涨/跌/炸板 (limit_list_d × 3)", self._fetch_limit_list_d),
            ("拉取连板信息 (kpl_list 今日)", self._fetch_kpl_today),
            ("拉取同花顺涨/跌/炸/连扳池 (limit_list_ths × 4)", self._fetch_limit_list_ths),
            ("拉取同花顺冲刺涨停 (limit_list_ths)", self._fetch_limit_sprint),
            ("拉取昨日涨停 (kpl_list 昨)", self._fetch_kpl_prev),
            ("拉取北向资金 (moneyflow_hsgt)", self._fetch_hsgt),
            ("拉取龙虎榜个股 (top_list)", self._fetch_top_list),
            ("拉取龙虎榜机构 (top_inst)", self._fetch_top_inst),
            ("拉取财联社个股异动 (cls_stock_shock)", self._fetch_cls_stock_shock),
            ("拉取财联社板块异动 (cls_market_shock)", self._fetch_cls_market_shock),
            ("拉取全市场涨跌家数 (daily)", self._fetch_market_breadth),
        ]
        for label, func in steps:
            progress(label)
            try:
                func(result)
            except Exception as exc:  # noqa: BLE001
                msg = f"{label} 失败: {exc}"
                _log.warning(msg, exc_info=True)
                result.warnings.append(msg)

        # 派生：全板块 5 日累计涨幅
        # 旧版仅算 Top N（top_sector_n），但 dc_daily 接口本来就拉全板块，
        # 写全量仅多数百次 UPDATE（毫秒级）→ GUI Top 20 / Bottom 10 都能用
        progress("派生全板块 5 日累计…")
        try:
            self._enrich_sector_5d_pct(result, top_n=0)
        except Exception as exc:  # noqa: BLE001
            msg = f"派生 5 日累计失败: {exc}"
            _log.warning(msg, exc_info=True)
            result.warnings.append(msg)

        result.api_call_count = self.client.call_count - api0
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    # ==================================================================
    # 缓存检测 & 清理
    # ==================================================================

    def _has_index_data(self, trade_date: str) -> bool:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM fact_index_daily WHERE trade_date = ? LIMIT 1",
                (trade_date,),
            ).fetchone()
        return row is not None

    def _purge_trade_date(self, trade_date: str) -> None:
        """把某交易日的所有 fact_* 数据清空（force_refresh=True 时调用）。"""
        tables = [
            "fact_index_daily",
            "fact_sector_daily",
            "fact_limit_stock",
            "fact_limit_sprint",
            "fact_hsgt_daily",
            "fact_top_list",
            "fact_top_inst",
            "fact_cls_stock_shock",
            "fact_cls_market_shock",
            "fact_market_breadth",
        ]
        with self.db.connect() as conn:
            for t in tables:
                conn.execute(
                    f"DELETE FROM {t} WHERE trade_date = ?", (trade_date,)
                )

    # ==================================================================
    # 各接口 fetch + ingest
    # ==================================================================

    # --- 1. index_daily × 7 ---

    def _fetch_index_daily(self, result: FetchResult) -> None:
        all_rows: List[dict] = []
        for ts_code, _key, _label in INDEX_CODES:
            try:
                rows = self.client.call(
                    "index_daily",
                    params={
                        "ts_code": ts_code,
                        "trade_date": result.trade_date,
                    },
                )
            except TushareError as exc:
                result.warnings.append(f"index_daily {ts_code}: {exc}")
                continue
            for r in rows:
                # 单位归一化：amount 千元 → 亿元
                r["__amount_yi__"] = _safe_div(r.get("amount"), 1e5)
            all_rows.extend(rows)

        n = self._ingest_index_daily(all_rows)
        result.ingested["fact_index_daily"] = n

    def _ingest_index_daily(self, rows: Sequence[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            payload.append(
                (
                    r.get("trade_date"),
                    r.get("ts_code"),
                    _to_float(r.get("close")),
                    _to_float(r.get("pct_chg")),
                    _to_float(r.get("__amount_yi__")),
                    _to_float(r.get("vol")),
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_index_daily "
                "(trade_date, ts_code, close, pct_chg, amount_yi, "
                " vol, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 2. dc_index → dim_sector ---

    def _fetch_sector_dict(self, result: FetchResult) -> None:
        rows = self.client.call(
            "dc_index",
            params={"trade_date": result.trade_date},
        )
        n = self._ingest_dim_sector(rows, result.trade_date)
        result.ingested["dim_sector"] = n
        result.ingested["__dc_index_count__"] = len(rows)

    def _ingest_dim_sector(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        """把 dc_index 返回的板块字典 upsert 进 dim_sector。

        ``dim_sector`` 既有 PK(ts_code) 又有 UNIQUE(name, src)，dc_index
        实际数据里可能两个 ts_code 共享同一 (name, src='dc')——例如同名概念
        在不同子源出现。这里采用 **两阶段写入**：

        1. ``INSERT OR IGNORE`` 把新 ts_code 写进去；命中 (name, src) 冲突
           的额外条目自动忽略（保留先到者）。
        2. 对所有 ts_code 单独 ``UPDATE last_seen_date``，刷新"最近一次出现"
           标记，给后续过滤已下市/重命名板块用。
        """
        if not rows:
            return 0

        seen_keys: set[Tuple[str, str]] = set()
        insert_payload: List[tuple] = []
        update_payload: List[tuple] = []
        for r in rows:
            ts_code = r.get("ts_code")
            name = r.get("name")
            if not ts_code or not name:
                continue
            # 同批内去重，避免一次 dc_index 自带重复 (name, src) 行
            key = (str(name), "dc")
            if key in seen_keys:
                continue
            seen_keys.add(key)

            insert_payload.append(
                (
                    ts_code,
                    name,
                    r.get("idx_type") or "概念板块",
                    "dc",
                    None,
                    trade_date,
                )
            )
            update_payload.append((trade_date, ts_code))

        if not insert_payload:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO dim_sector "
                "(ts_code, name, idx_type, src, list_date, last_seen_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                insert_payload,
            )
            conn.executemany(
                "UPDATE dim_sector SET last_seen_date = ? "
                "WHERE ts_code = ?",
                update_payload,
            )
            inserted = conn.execute(
                "SELECT COUNT(*) FROM dim_sector WHERE src = 'dc'"
            ).fetchone()[0]
        return int(inserted)

    # --- 3. moneyflow_ind_dc → fact_sector_daily ---

    def _fetch_sector_moneyflow(self, result: FetchResult) -> None:
        rows = self.client.call(
            "moneyflow_ind_dc",
            params={
                "trade_date": result.trade_date,
                "content_type": "概念",
            },
        )
        n = self._ingest_sector_daily(rows, result.trade_date)
        result.ingested["fact_sector_daily"] = n

    def _ingest_sector_daily(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        if not rows:
            return 0
        # 兜底：把本批次自带的 ts_code+name 全部 INSERT OR IGNORE 进
        # dim_sector，避免外键失败（dc_index 可能漏拉，或字典里 src!='dc'）
        dim_payload: List[tuple] = []
        seen: set[str] = set()
        for r in rows:
            ts_code = r.get("ts_code")
            name = r.get("name")
            if not ts_code or not name or ts_code in seen:
                continue
            seen.add(str(ts_code))
            dim_payload.append(
                (ts_code, name, "概念板块", "dc", None, trade_date)
            )

        payload = []
        for r in rows:
            ts_code = r.get("ts_code")
            if not ts_code:
                continue
            payload.append(
                (
                    trade_date,
                    ts_code,
                    _to_float(r.get("pct_change")),
                    _safe_div(r.get("net_amount"), 1e8),
                    _safe_div(r.get("buy_elg_amount"), 1e8),
                    _safe_div(r.get("buy_lg_amount"), 1e8),
                    None,  # limit_up_count 后续合并 kpl 后再填（M1b 留空）
                    None,  # pct_chg_5d 派生步骤填
                    _to_int(r.get("rank")),
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            if dim_payload:
                conn.executemany(
                    "INSERT OR IGNORE INTO dim_sector "
                    "(ts_code, name, idx_type, src, "
                    " list_date, last_seen_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    dim_payload,
                )
            conn.executemany(
                "INSERT OR REPLACE INTO fact_sector_daily "
                "(trade_date, ts_code, pct_chg, main_net_yi, "
                " main_elg_yi, main_lg_yi, limit_up_count, "
                " pct_chg_5d, rank_today, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 4. limit_list_d × 3 ---

    def _fetch_limit_list_d(self, result: FetchResult) -> None:
        total = 0
        for limit_type in LIMIT_TYPES:
            try:
                rows = self.client.call(
                    "limit_list_d",
                    params={
                        "trade_date": result.trade_date,
                        "limit_type": limit_type,
                    },
                )
            except TushareError as exc:
                result.warnings.append(f"limit_list_d {limit_type}: {exc}")
                continue
            total += self._ingest_limit_stock(
                rows, result.trade_date, limit_type
            )
        result.ingested["fact_limit_stock_initial"] = total

    def _ingest_limit_stock(
        self, rows: Sequence[dict], trade_date: str, limit_type: str
    ) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            ts_code = r.get("ts_code")
            if not ts_code:
                continue
            payload.append(
                (
                    trade_date,
                    ts_code,
                    limit_type,
                    None,  # status，留给 kpl_list 合并
                    None,  # cons_nums，同上
                    None,  # theme，同上
                    r.get("first_time") or r.get("last_time"),
                    _to_int(r.get("limit_times")),
                    _to_float(r.get("close")),
                    _to_float(r.get("pct_chg")),
                    _safe_div(r.get("fd_amount"), 1e8),
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_limit_stock "
                "(trade_date, ts_code, limit_type, status, cons_nums, "
                " theme, limit_up_time, open_times, close, pct_chg, "
                " fd_amount_yi, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 5. kpl_list 今日（合并到 fact_limit_stock） ---

    def _fetch_kpl_today(self, result: FetchResult) -> None:
        rows = self.client.call(
            "kpl_list",
            params={
                "trade_date": result.trade_date,
                "tag": "涨停",
            },
        )
        n = self._merge_kpl_into_limit_stock(rows, result.trade_date)
        result.ingested["fact_limit_stock_kpl_merged"] = n
        result.ingested["__kpl_today_count__"] = len(rows)

    def _merge_kpl_into_limit_stock(
        self, kpl_rows: Sequence[dict], trade_date: str
    ) -> int:
        """把 kpl_list 的 status / cons_nums / theme 合并进 fact_limit_stock。

        kpl_list 主键 (trade_date, ts_code)；目标表主键含 limit_type='U'。
        """
        if not kpl_rows:
            return 0
        from services.market.metrics import parse_cons_nums

        payload: List[Tuple[Any, ...]] = []
        for r in kpl_rows:
            ts_code = r.get("ts_code")
            if not ts_code:
                continue
            payload.append(
                (
                    r.get("status"),
                    parse_cons_nums(r),
                    r.get("theme"),
                    r.get("lu_time") or r.get("first_time"),
                    _to_int(r.get("open_times")),
                    trade_date,
                    ts_code,
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "UPDATE fact_limit_stock SET "
                "  status = COALESCE(?, status), "
                "  cons_nums = COALESCE(?, cons_nums), "
                "  theme = COALESCE(?, theme), "
                "  limit_up_time = COALESCE(?, limit_up_time), "
                "  open_times = COALESCE(?, open_times) "
                "WHERE trade_date = ? AND ts_code = ? AND limit_type = 'U'",
                payload,
            )
            updated = conn.execute(
                "SELECT COUNT(*) FROM fact_limit_stock "
                "WHERE trade_date = ? AND limit_type = 'U' "
                "AND cons_nums IS NOT NULL",
                (trade_date,),
            ).fetchone()[0]
        return int(updated)

    # --- 6. kpl_list 昨日（仅内存） ---

    def _fetch_kpl_prev(self, result: FetchResult) -> None:
        rows = self.client.call(
            "kpl_list",
            params={
                "trade_date": result.prev_trade_date,
                "tag": "涨停",
            },
        )
        result.kpl_prev_rows = list(rows)
        result.ingested["__kpl_prev_count__"] = len(rows)

    # --- 7. moneyflow_hsgt（北向，T 日空兜底 T-1） ---

    def _fetch_hsgt(self, result: FetchResult) -> None:
        actual_date = result.trade_date
        is_delayed = False
        rows = self.client.call(
            "moneyflow_hsgt",
            params={"trade_date": result.trade_date},
        )
        if not rows:
            # 兜底：T 日空 → 取 prev
            rows = self.client.call(
                "moneyflow_hsgt",
                params={"trade_date": result.prev_trade_date},
            )
            if rows:
                actual_date = result.prev_trade_date
                is_delayed = True
                result.warnings.append(
                    f"北向资金 T 日({result.trade_date}) 无数据，"
                    f"已回退到 T-1({result.prev_trade_date})"
                )

        n = self._ingest_hsgt(
            rows, request_date=result.trade_date,
            actual_date=actual_date, is_delayed=is_delayed,
        )
        result.ingested["fact_hsgt_daily"] = n
        result.hsgt_data_date = actual_date if rows else None
        result.hsgt_is_delayed = is_delayed

    def _ingest_hsgt(
        self,
        rows: Sequence[dict],
        *,
        request_date: str,
        actual_date: str,
        is_delayed: bool,
    ) -> int:
        if not rows:
            return 0
        r = rows[0]
        north = _safe_div(r.get("north_money"), 1e4)
        south = _safe_div(r.get("south_money"), 1e4)
        raw = json.dumps(_strip_internal(r), ensure_ascii=False)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fact_hsgt_daily "
                "(trade_date, actual_date, is_delayed, "
                " north_net_yi, south_net_yi, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    request_date,
                    actual_date,
                    1 if is_delayed else 0,
                    north,
                    south,
                    raw,
                ),
            )
        return 1

    # --- 8. top_list ---

    def _fetch_top_list(self, result: FetchResult) -> None:
        rows = self.client.call(
            "top_list",
            params={"trade_date": result.trade_date},
        )
        # 过滤可转债：ts_code 以 11 / 12 开头是转债
        rows = [r for r in rows if not _is_convertible_bond(r.get("ts_code"))]
        n = self._ingest_top_list(rows, result.trade_date)
        result.ingested["fact_top_list"] = n

    def _ingest_top_list(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        if not rows:
            return 0
        payload = []
        seen: set = set()
        for r in rows:
            ts_code = r.get("ts_code")
            reason = r.get("reason") or ""
            if not ts_code:
                continue
            key = (trade_date, ts_code, reason)
            if key in seen:
                continue
            seen.add(key)
            payload.append(
                (
                    trade_date,
                    ts_code,
                    reason,
                    _safe_div(r.get("net_amount"), 1e8),
                    _to_float(r.get("pct_change")),
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_top_list "
                "(trade_date, ts_code, rank_reason, net_amount_yi, "
                " pct_chg, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 9. top_inst ---

    def _fetch_top_inst(self, result: FetchResult) -> None:
        rows = self.client.call(
            "top_inst",
            params={"trade_date": result.trade_date},
        )
        n = self._ingest_top_inst(rows, result.trade_date)
        result.ingested["fact_top_inst"] = n

    def _ingest_top_inst(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        if not rows:
            return 0
        payload = []
        seen: set = set()
        for r in rows:
            ts_code = r.get("ts_code")
            exalter = r.get("exalter") or ""
            side_raw = str(r.get("side") or "").strip()
            # 0 = 买，1 = 卖（按 audit §3.10）
            if side_raw in ("0", "buy", "B", "BUY"):
                side = "buy"
            elif side_raw in ("1", "sell", "S", "SELL"):
                side = "sell"
            else:
                # 未知 side 不入库（避免主键冲突）
                continue
            if not ts_code or not exalter:
                continue
            key = (trade_date, ts_code, exalter, side)
            if key in seen:
                continue
            seen.add(key)
            payload.append(
                (
                    trade_date,
                    ts_code,
                    exalter,
                    side,
                    _safe_div(r.get("net_buy"), 1e8),
                    _safe_div(r.get("buy"), 1e8),
                    _safe_div(r.get("sell"), 1e8),
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_top_inst "
                "(trade_date, ts_code, exalter, side, "
                " net_buy_yi, buy_amount_yi, sell_amount_yi, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 10. cls_stock_shock（财联社涨停个股催化原因 + 板块映射） ---

    def _fetch_cls_stock_shock(self, result: FetchResult) -> None:
        rows = self.client.call(
            "cls_stock_shock",
            params={"trade_date": result.trade_date},
        )
        n = self._ingest_cls_stock_shock(rows, result.trade_date)
        result.ingested["fact_cls_stock_shock"] = n

    def _ingest_cls_stock_shock(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        if not rows:
            return 0
        payload: List[tuple] = []
        seen: set = set()
        for r in rows:
            ts_code = r.get("ts_code")
            shock_time = r.get("time") or ""
            if not ts_code:
                continue
            key = (trade_date, ts_code, shock_time)
            if key in seen:
                continue
            seen.add(key)
            # plate 是 JSON 字符串，直接整字段透传（已是 str）
            plate_raw = r.get("plate") or ""
            payload.append(
                (
                    trade_date,
                    ts_code,
                    r.get("reason") or None,
                    str(plate_raw) if plate_raw else None,
                    shock_time or None,
                    json.dumps(_strip_internal(r), ensure_ascii=False),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_cls_stock_shock "
                "(trade_date, ts_code, reason, plate_json, "
                " shock_time, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 11. cls_market_shock（财联社板块异动时间线） ---

    def _fetch_cls_market_shock(self, result: FetchResult) -> None:
        rows = self.client.call(
            "cls_market_shock",
            params={"trade_date": result.trade_date},
        )
        n = self._ingest_cls_market_shock(rows, result.trade_date)
        result.ingested["fact_cls_market_shock"] = n

    def _ingest_cls_market_shock(
        self, rows: Sequence[dict], trade_date: str
    ) -> int:
        """同一板块同一 status 可能多次异动，按 (sector, status) 聚合:

        * ``first_shock_time`` = 该 (sector, status) 最早一次 c_time
        * ``shock_count``      = 该 (sector, status) 的事件数
        """
        if not rows:
            return 0
        from collections import defaultdict

        agg: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
            lambda: {"first_time": None, "count": 0, "raw_first": None}
        )
        for r in rows:
            name = (r.get("name") or "").strip()
            status = (r.get("status") or "").strip().lower()
            c_time = (r.get("c_time") or "").strip()
            if not name or status not in ("up", "down"):
                continue
            entry = agg[(name, status)]
            if entry["first_time"] is None or c_time < entry["first_time"]:
                entry["first_time"] = c_time
                entry["raw_first"] = r
            entry["count"] += 1

        payload: List[tuple] = []
        for (name, status), entry in agg.items():
            first_time = entry["first_time"]
            # 提取 HH:MM:SS 部分（c_time 是 'YYYY-MM-DD HH:MM:SS'）
            shock_time = (
                first_time.split(" ")[-1] if first_time else None
            )
            payload.append(
                (
                    trade_date,
                    name,
                    shock_time,
                    int(entry["count"]),
                    status,
                    json.dumps(
                        _strip_internal(entry["raw_first"] or {}),
                        ensure_ascii=False,
                    ),
                )
            )
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_cls_market_shock "
                "(trade_date, sector_name, first_shock_time, "
                " shock_count, status, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # --- 12. daily 全市场涨跌家数 ---

    def _fetch_market_breadth(self, result: FetchResult) -> None:
        """全市场涨/跌家数。

        调 ``tushare.daily(trade_date=...)`` 一次（默认 6000 行上限，
        A 股全市场 ~5400 只够用）。按 pct_chg 分桶聚合后写
        ``fact_market_breadth`` 单行；本字段不需要历史明细，重拉即可。
        """
        try:
            rows = self.client.call(
                "daily",
                params={"trade_date": result.trade_date},
            )
        except TushareError as exc:
            result.warnings.append(f"daily(market_breadth): {exc}")
            return
        if not rows:
            result.warnings.append(
                f"daily({result.trade_date}) 返回空（节假日 / 接口异常）"
            )
            return

        adv = dec = unc = adv5 = dec5 = total = 0
        for r in rows:
            pct = r.get("pct_chg")
            if pct is None:
                continue
            try:
                p = float(pct)
            except (TypeError, ValueError):
                continue
            total += 1
            if p > 0:
                adv += 1
                if p >= 5.0:
                    adv5 += 1
            elif p < 0:
                dec += 1
                if p <= -5.0:
                    dec5 += 1
            else:
                unc += 1

        if total <= 0:
            result.warnings.append("daily 聚合后 total=0，跳过 market_breadth 入库")
            return

        adv_pct = round(adv / total, 4) if total > 0 else None
        result.market_breadth = {
            "advance": adv,
            "decline": dec,
            "unchanged": unc,
            "advance_5": adv5,
            "decline_5": dec5,
            "advance_pct": adv_pct,
            "total": total,
        }
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fact_market_breadth "
                "(trade_date, advance, decline, unchanged, "
                " advance_5, decline_5, advance_pct, total, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.trade_date,
                    adv, dec, unc, adv5, dec5, adv_pct, total,
                    json.dumps(result.market_breadth, ensure_ascii=False),
                ),
            )
        result.ingested["fact_market_breadth"] = 1

    # --- 13. dc_daily 5 日累计派生 ---

    def _enrich_sector_5d_pct(
        self, result: FetchResult, *, top_n: int = 0
    ) -> None:
        """对 ``fact_sector_daily`` 当日板块，取 5 日累计涨幅写回。

        策略：dc_daily 接口本来就拉全板块全量，按 ts_code 算后批量 UPDATE。
        实际 API 调用 1 次；UPDATE 行数 = 实际能算出 5 日涨幅的板块数（~500）。

        Args:
            result: FetchResult，必须含 trade_date。
            top_n: 派生范围限制。
                * ``top_n <= 0``（默认）→ 全板块都算（推荐，给 GUI Top 20 / Bottom 10
                  都填上 pct_chg_5d，写库成本毫秒级）
                * ``top_n > 0`` → 仅当日 Top N 板块（旧行为，保留向后兼容）
        """
        top_codes: Optional[set] = None
        if top_n and top_n > 0:
            with self.db.connect(readonly=True) as conn:
                top_rows = conn.execute(
                    "SELECT ts_code FROM fact_sector_daily "
                    "WHERE trade_date = ? "
                    "ORDER BY pct_chg DESC NULLS LAST LIMIT ?",
                    (result.trade_date, top_n),
                ).fetchall()
            top_codes = {r[0] for r in top_rows}
            if not top_codes:
                return

        # 估算 5 个交易日的自然日窗口（保险起见取 12 天，覆盖一个含小长假）
        from datetime import datetime, timedelta

        d_end = datetime.strptime(result.trade_date, "%Y%m%d")
        start_str = (d_end - timedelta(days=12)).strftime("%Y%m%d")
        try:
            rows = self.client.call(
                "dc_daily",
                params={
                    "start_date": start_str,
                    "end_date": result.trade_date,
                },
            )
        except TushareError as exc:
            result.warnings.append(f"dc_daily(派生): {exc}")
            return

        from collections import defaultdict
        from services.market.metrics import calc_sector_5d_pct

        by_code: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            code = r.get("ts_code")
            if not isinstance(code, str):
                continue
            if top_codes is not None and code not in top_codes:
                continue
            by_code[code].append(r)

        payload: List[Tuple[Any, ...]] = []
        for code, history in by_code.items():
            pct5 = calc_sector_5d_pct(history)
            if pct5 is not None:
                payload.append((pct5, result.trade_date, code))

        if not payload:
            return
        with self.db.connect() as conn:
            conn.executemany(
                "UPDATE fact_sector_daily SET pct_chg_5d = ? "
                "WHERE trade_date = ? AND ts_code = ?",
                payload,
            )
        result.ingested["fact_sector_daily_5d_enriched"] = len(payload)

    # ==================================================================
    # 维度表（独立于日线流水线，按需手动 / 启动时自动）
    # ==================================================================

    def fetch_dim_stock_full(
        self,
        *,
        include_delisted: bool = False,
    ) -> Dict[str, int]:
        """一次性拉全市场股票基础信息 → 入库 dim_stock。

        Tushare 接口: ``stock_basic``（无 trade_date 参数，是字典型接口）
        实际 API 调用: 1 次（``L``）+ 可选 1 次（``D``）

        Args:
            include_delisted: True 时同时拉退市股（``list_status='D'``），
                让历史龙虎榜 ts_code 也能反查到名字（已下市的）。

        Returns:
            ``{"L": 在市股数, "D": 退市股数, "total": 写入总行数}``
        """
        out: Dict[str, int] = {"L": 0, "D": 0, "total": 0}

        statuses = ["L"]
        if include_delisted:
            statuses.append("D")

        for status in statuses:
            try:
                rows = self.client.call(
                    "stock_basic",
                    params={"list_status": status},
                    fields="ts_code,name,market,industry,list_date",
                )
            except TushareError as exc:
                _log.warning("stock_basic(%s) 拉取失败: %s", status, exc)
                continue
            n = self._ingest_dim_stock(rows)
            out[status] = n
            out["total"] += n
            _log.info("dim_stock 写入 %s 状态 %d 只", status, n)

        return out

    def _ingest_dim_stock(self, rows: Sequence[dict]) -> int:
        """把 stock_basic 返回行 upsert 进 dim_stock。"""
        payload: List[tuple] = []
        for r in rows:
            ts_code = r.get("ts_code")
            name = r.get("name")
            if not ts_code or not name:
                continue
            payload.append((
                str(ts_code),
                str(name),
                str(r.get("market") or "") or None,
                str(r.get("industry") or "") or None,
                str(r.get("list_date") or "") or None,
            ))
        if not payload:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO dim_stock "
                "(ts_code, name, market, industry, list_date) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)


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
    if f != f:  # NaN
        return None
    return f


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_div(value: Any, divisor: float) -> Optional[float]:
    """单位换算专用：``value / divisor``，缺失/异常返回 None。"""
    f = _to_float(value)
    if f is None or divisor == 0:
        return None
    return round(f / divisor, 4)


def _is_convertible_bond(ts_code: Optional[str]) -> bool:
    """根据代码前缀判断是否可转债。

    SH: 110xxx / 113xxx / 118xxx
    SZ: 123xxx / 128xxx
    """
    if not ts_code:
        return False
    code = str(ts_code).split(".")[0]
    return (
        code.startswith("110")
        or code.startswith("113")
        or code.startswith("118")
        or code.startswith("123")
        or code.startswith("128")
    )


def _strip_internal(d: Dict[str, Any]) -> Dict[str, Any]:
    """从 dict 里剔除 ``__xxx__`` 内部临时字段，保留 raw_json 原貌。"""
    return {
        k: v
        for k, v in d.items()
        if not (k.startswith("__") and k.endswith("__"))
    }


__all__ = [
    "INDEX_CODES",
    "FetchResult",
    "TushareMarketFetcher",
]
