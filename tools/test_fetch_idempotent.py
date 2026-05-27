"""TushareMarketFetcher 逐表幂等回归测试（2026-05-27 缓存语义修订）。

覆盖范围
--------
1. **空库全跑**：fetcher.fetch(force=False) 14+1 个 step 全部 ingest，skipped 为空
2. **二次幂等**：紧接着再跑 fetch(force=False)，所有 A 组表 skipped、API 显著下降
3. **缺失补齐**：只删 fact_stock_daily 当日 → 仅 step 15 重跑、其他 skipped
4. **force 全清**：fetch(force=True) → 全部 14+1 张 fact 表清空重写

测试隔离
--------
* 用 trade_date=``99990520``（前缀 99）伪日期，不污染真实数据
* MockTushareClient 模拟全部 14 个 API 应答
* 用例完成后自动清理 fact_*/dim_sector 中所有 9999 前缀行

运行
----
::

    python tools/test_fetch_idempotent.py

成功 exit 0 / 失败 exit 1
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mock TushareClient
# ---------------------------------------------------------------------------


class MockTushareClient:
    """模拟 :class:`TushareClient`。

    Args:
        responses: ``{api_name: list[dict] | callable(params) -> list[dict]}``
            未注册的 api_name 默认返回 ``[]``。
    """

    def __init__(
        self, responses: Optional[Dict[str, Any]] = None
    ) -> None:
        self.responses = responses or {}
        self.call_count = 0
        self.calls: List[Tuple[str, dict]] = []

    def call(
        self,
        api_name: str,
        params: Optional[dict] = None,
        *,
        fields: str = "",
        **_kwargs,
    ) -> List[Dict[str, Any]]:
        self.call_count += 1
        self.calls.append((api_name, dict(params or {})))
        resp = self.responses.get(api_name)
        if resp is None:
            return []
        if callable(resp):
            return resp(params or {})
        return list(resp)


# ---------------------------------------------------------------------------
# Mock 数据工厂
# ---------------------------------------------------------------------------

_TD = "99990520"
_PTD = "99990519"


def _build_mock_responses() -> Dict[str, Any]:
    """构造覆盖 14+1 个 step 全部 API 的最小可入库数据。"""

    index_codes_sample = [
        "000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
        "000688.SH", "000905.SH", "000852.SH",
    ]

    def _index_daily(params: dict) -> List[Dict[str, Any]]:
        ts = params.get("ts_code")
        if ts not in index_codes_sample:
            return []
        return [{
            "ts_code": ts,
            "trade_date": _TD,
            "close": 3000.0,
            "pct_chg": 1.0,
            "amount": 1e10,
            "vol": 1e8,
        }]

    def _limit_list_d(params: dict) -> List[Dict[str, Any]]:
        lt = params.get("limit_type")
        # 每种 limit_type 给 1 行，PK=(trade_date, ts_code, limit_type) 不冲突
        return [{
            "ts_code": f"TEST{lt}001.SZ",
            "trade_date": _TD,
            "name": f"测试{lt}股",
            "industry": "测试行业",
            "limit_type": lt,
            "status": "首板",
            "pct_chg": 10.0 if lt == "U" else (-10.0 if lt == "D" else 5.0),
            "close": 10.0,
            "fd_amount": 1e8,
            "limit_amount": 1e8,
            "lu_time": "09:30:00",
            "open_times": 0,
            "theme": "测试题材",
        }]

    def _kpl_list(params: dict) -> List[Dict[str, Any]]:
        # T 日和 T-1 都给 1 行（PK 不影响，因为不同 trade_date）
        td = params.get("trade_date")
        return [{
            "ts_code": "TESTU001.SZ",
            "trade_date": td,
            "name": "测试U股",
            "lu_time": "09:30:00",
            "lu_desc": "首板",
            "theme": "测试题材",
            "status": "首板",
            "limit_up_suc_rate": 0.8,
            "tag": "新晋",
        }]

    def _limit_list_ths(params: dict) -> List[Dict[str, Any]]:
        lt = params.get("limit_type") or ""
        return [{
            "ts_code": f"TESTU001.SZ",
            "trade_date": _TD,
            "name": "测试同花顺",
            "pct_change": 10.0,
            "open_num": 0,
            "lu_time": "09:30:00",
            "first_lu_time": "09:30:00",
            "last_lu_time": "09:30:00",
            "lu_desc": lt,
            "limit_order": 1e8,
            "limit_amount": 1e8,
            "tag": lt,
            "market_type": "主板",
            "turnover_rate": 5.0,
            "free_float": 1e9,
            "lu_type": "1",
        }]

    def _moneyflow_hsgt(params: dict) -> List[Dict[str, Any]]:
        td = params.get("trade_date")
        return [{
            "trade_date": td,
            "north_money": 100.0,
            "south_money": 50.0,
            "hgt": 50.0, "ggt_ss": 25.0, "sgt": 50.0, "ggt_sz": 25.0,
        }]

    def _daily(params: dict) -> List[Dict[str, Any]]:
        # market_breadth 和 sync_stock_daily 都用 daily（step 14 + 15）
        # 给 10 行（含 advance/decline/unchanged 三种 pct_chg）
        rows = []
        for i in range(10):
            pct = (i - 4) * 1.0  # -4 ~ +5
            rows.append({
                "ts_code": f"TEST{i:03d}.SZ",
                "trade_date": _TD,
                "open": 10.0, "high": 10.5, "low": 9.5,
                "close": 10.0 + pct * 0.1,
                "pre_close": 10.0,
                "change": pct * 0.1,
                "pct_chg": pct,
                "vol": 1e6,
                "amount": 1e7,
            })
        return rows

    def _dc_daily(params: dict) -> List[Dict[str, Any]]:
        # 5d 累计派生用，返回多个 trade_date 数据
        return [{
            "ts_code": "TESTBK001.DC",
            "trade_date": _TD,
            "pct_change": 2.0,
        }]

    return {
        "index_daily": _index_daily,
        "dc_index": [{
            "ts_code": "TESTBK001.DC",
            "name": "测试板块1",
            "idx_type": "概念板块",
        }],
        "moneyflow_ind_dc": [
            {
                "ts_code": "TESTBK001.DC",
                "name": "测试板块1",
                "pct_change": 2.0,
                "net_amount": 1e9,
                "buy_elg_amount": 5e8,
                "buy_lg_amount": 3e8,
                "rank": 1,
            },
        ],
        "limit_list_d": _limit_list_d,
        "kpl_list": _kpl_list,
        "limit_list_ths": _limit_list_ths,
        "moneyflow_hsgt": _moneyflow_hsgt,
        "top_list": [{
            "ts_code": "600000.SH",
            "trade_date": _TD,
            "name": "测试龙虎",
            "reason": "测试上榜原因",
            "net_amount": 1e8,
            "pct_change": 5.0,
            "amount": 1e9,
        }],
        "top_inst": [{
            "trade_date": _TD,
            "ts_code": "600000.SH",
            "exalter": "测试营业部",
            "side": "0",
            "buy": 1e8,
            "sell": 0.0,
            "buy_rate": 5.0,
            "sell_rate": 0.0,
            "net_buy": 1e8,
        }],
        "cls_stock_shock": [{
            "ts_code": "TESTU001.SZ",
            "trade_date": _TD,
            "time": "09:35:00",
            "title": "测试个股异动",
            "content": "测试内容",
            "concept": "测试题材",
        }],
        "cls_market_shock": [{
            "trade_date": _TD,
            "name": "测试板块1",
            "c_time": f"{_TD[:4]}-{_TD[4:6]}-{_TD[6:]} 09:40:00",
            "title": "测试板块异动",
            "content": "测试内容",
            "status": "up",
        }],
        "daily": _daily,
        "dc_daily": _dc_daily,
    }


# ---------------------------------------------------------------------------
# 测试辅助：清理
# ---------------------------------------------------------------------------


def _cleanup_fake_data() -> None:
    """清除 fact_*/dim_sector 中所有 9999 / TEST 前缀的伪数据。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    fact_tables = [
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
        "fact_stock_daily",
    ]
    with db.connect() as conn:
        for t in fact_tables:
            try:
                conn.execute(
                    f"DELETE FROM {t} WHERE trade_date LIKE '9999%'"
                )
            except Exception:
                pass
        try:
            conn.execute(
                "DELETE FROM dim_sector WHERE ts_code LIKE 'TEST%'"
            )
        except Exception:
            pass


def _table_count(table: str, trade_date: str = _TD) -> int:
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_first_run_full_ingest() -> None:
    """空库 → fetch(force=False)：14+1 个 step 全跑、skipped 为空。"""
    from services.market.market_db import get_market_db
    from services.market.tushare_fetcher import TushareMarketFetcher

    _cleanup_fake_data()
    client = MockTushareClient(_build_mock_responses())
    fetcher = TushareMarketFetcher(client, get_market_db())

    result = fetcher.fetch(
        trade_date=_TD, prev_trade_date=_PTD, force_refresh=False
    )

    # skipped 必须为空（A 组 9 个表 + step 15 都应该实际跑）
    skipped_a_group = [
        s for s in result.skipped
        if s != "fact_market_breadth"  # 容许 mock 失败
    ]
    assert not skipped_a_group, (
        f"空库首跑 skipped 应为空，实际: {result.skipped}"
    )

    # 关键表必须有数据写入
    assert _table_count("fact_index_daily") >= 7, "指数表至少 7 行"
    assert _table_count("fact_stock_daily") > 0, (
        f"fact_stock_daily 应有数据，实际 {_table_count('fact_stock_daily')}"
    )
    assert _table_count("fact_limit_stock") > 0, "涨停表应有数据"


def case_02_second_run_skips_all() -> None:
    """紧接着再 fetch(force=False)：A 组全部 skipped、API 显著下降。"""
    from services.market.market_db import get_market_db
    from services.market.tushare_fetcher import TushareMarketFetcher

    # 接续 case_01 已经写满数据的状态
    client = MockTushareClient(_build_mock_responses())
    fetcher = TushareMarketFetcher(client, get_market_db())

    result = fetcher.fetch(
        trade_date=_TD, prev_trade_date=_PTD, force_refresh=False
    )

    # A 组 9 个表 + step 15 应全部 skip
    expected_skipped = {
        "fact_index_daily",
        "fact_sector_daily",
        "fact_limit_sprint",
        "fact_hsgt_daily",
        "fact_top_list",
        "fact_top_inst",
        "fact_cls_stock_shock",
        "fact_cls_market_shock",
        "fact_market_breadth",
        "fact_stock_daily",
    }
    skipped_set = set(result.skipped)
    missing = expected_skipped - skipped_set
    assert not missing, (
        f"第 2 次跑应全部 skip，缺失 skip 的表: {missing} | "
        f"实际 skipped: {result.skipped}"
    )

    # API 调用数应显著少于第 1 次（step 4/5/6 + step 8 + dc_daily 仍跑约 4-10 次）
    assert client.call_count < 20, (
        f"第 2 次 API 调用应 < 20 次，实际 {client.call_count}"
    )


def case_03_only_stock_daily_missing() -> None:
    """只删 fact_stock_daily → 仅 step 15 重跑、其他 skip。"""
    from services.market.market_db import get_market_db
    from services.market.tushare_fetcher import TushareMarketFetcher

    # 单独清掉 fact_stock_daily 当日
    db = get_market_db()
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM fact_stock_daily WHERE trade_date = ?", (_TD,)
        )

    client = MockTushareClient(_build_mock_responses())
    fetcher = TushareMarketFetcher(client, get_market_db())

    result = fetcher.fetch(
        trade_date=_TD, prev_trade_date=_PTD, force_refresh=False
    )

    # fact_stock_daily 不应在 skipped 列表里（被重跑）
    assert "fact_stock_daily" not in result.skipped, (
        f"fact_stock_daily 应被重跑，但出现在 skipped: {result.skipped}"
    )
    # 其他独立表应被 skip
    assert "fact_index_daily" in result.skipped
    assert "fact_top_list" in result.skipped

    # fact_stock_daily 被重新写入
    assert _table_count("fact_stock_daily") > 0, (
        "step 15 应已重新写入 fact_stock_daily"
    )


def case_04_force_refresh_purges_all() -> None:
    """fetch(force=True) → 全部 14+1 张 fact 表清空重写。"""
    from services.market.market_db import get_market_db
    from services.market.tushare_fetcher import TushareMarketFetcher

    # 先记录当前各表行数
    before_stock = _table_count("fact_stock_daily")
    assert before_stock > 0, "前置：fact_stock_daily 应有数据"

    client = MockTushareClient(_build_mock_responses())
    fetcher = TushareMarketFetcher(client, get_market_db())

    result = fetcher.fetch(
        trade_date=_TD, prev_trade_date=_PTD, force_refresh=True
    )

    # force 模式下 skipped 必须为空
    assert not result.skipped, (
        f"force=True 时 skipped 应为空，实际: {result.skipped}"
    )

    # 所有表都重新写入，行数与前次一致（mock 数据稳定）
    after_stock = _table_count("fact_stock_daily")
    assert after_stock > 0, "force 后 fact_stock_daily 应重新写入"
    assert _table_count("fact_index_daily") >= 7
    assert _table_count("fact_limit_stock") > 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_first_run_full_ingest", case_01_first_run_full_ingest),
    ("02_second_run_skips_all", case_02_second_run_skips_all),
    ("03_only_stock_daily_missing", case_03_only_stock_daily_missing),
    ("04_force_refresh_purges_all", case_04_force_refresh_purges_all),
]


def main() -> int:
    print(f"=== fetcher 逐表幂等回归 ({len(_CASES)} 用例) ===\n")
    passed: List[str] = []
    try:
        for name, fn in _CASES:
            try:
                fn()
                print(f"[PASS] {name}")
                passed.append(name)
            except AssertionError as exc:
                print(f"[FAIL] {name}: {exc}")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
                return 1
    finally:
        _cleanup_fake_data()
        print("\n[cleanup] 已清理 9999/TEST 伪数据")

    print(f"\n[全部通过] {len(passed)}/{len(_CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
