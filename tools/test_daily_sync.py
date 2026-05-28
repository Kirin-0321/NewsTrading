"""Phase 1 行情同步回归测试（8 用例，全部走 mock client，不耗 Tushare 配额）。

覆盖范围
--------
1. next_trade_date 节假日跳过（劳动节、国庆、春节）
2. trade_dates_between 区间正确性（跳周末 + 节假日）
3. sync_stock_daily 单日同步行数 >= mock 行数（默认 5400）
4. sync_stock_daily 幂等（重复跑不增行数，且第 2 次 skipped=True）
5. sync_stock_daily force=True 重写（先 DELETE 再 INSERT）
6. sync_sector_daily **5 源** 入库：dc 三类（概念/行业/地域）
   + ths 行业（moneyflow_ind_ths）+ ths 概念（moneyflow_cnt_ths）共 5 次 API
7. attached_dbs 跨库 JOIN：从 ai_inference 查 market.fact_stock_daily
   不报错 LIMIT 1
8. dim_sector 5 种 idx_type/src 组合都正确写入 + ths 行业 vs 概念字段差异
   （industry vs name）+ ths 资金流字段（无 elg/lg）验证

测试隔离
--------
* 用例 3/4/5/6 写入的 trade_date 用前缀 ``99`` 的伪日期（``99990501`` 等），
  保证不会与真实 Tushare 数据冲突；用例结尾自动清理。
* dim_trade_calendar 用 INSERT OR REPLACE 注入假节假日，测试完清理。

运行
----
::

    python tools/test_daily_sync.py

成功输出 [全部通过]，失败抛 AssertionError 并 exit(1)。
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
    """模拟 :class:`TushareClient`，按预制 dict 应答。

    Args:
        responses: ``{api_name: list[dict] | callable(params) -> list[dict]}``
            未注册的 api_name 默认返回 ``[]``。
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
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
# 测试辅助：日历伪造
# ---------------------------------------------------------------------------


_FAKE_CALENDAR_HOLIDAYS = [
    # (cal_date, is_open, pretrade_date)
    # 模拟劳动节 5/1-5/5 五连休
    ("99990428", 1, "99990427"),  # Tue
    ("99990429", 1, "99990428"),  # Wed
    ("99990430", 1, "99990429"),  # Thu
    ("99990501", 0, "99990430"),  # Fri  劳动节
    ("99990502", 0, "99990430"),  # Sat
    ("99990503", 0, "99990430"),  # Sun
    ("99990504", 0, "99990430"),  # Mon  劳动节
    ("99990505", 0, "99990430"),  # Tue  劳动节
    ("99990506", 1, "99990430"),  # Wed
    ("99990507", 1, "99990506"),  # Thu
    ("99990508", 1, "99990507"),  # Fri
    ("99990509", 0, "99990508"),  # Sat
    ("99990510", 0, "99990508"),  # Sun
    ("99990511", 1, "99990508"),  # Mon
]


def _seed_fake_calendar() -> None:
    """把假节假日写入 dim_trade_calendar。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO dim_trade_calendar "
            "(trade_date, is_open, pretrade_date, cal_date) "
            "VALUES (?, ?, ?, ?)",
            [(d, op, ptd, d) for d, op, ptd in _FAKE_CALENDAR_HOLIDAYS],
        )


def _cleanup_fake_data() -> None:
    """清除所有以 ``9999`` 开头的伪测试数据。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM dim_trade_calendar WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_stock_daily WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_sector_daily WHERE trade_date LIKE '9999%'"
        )
        # 同时清理假 ts_code（防止污染 dim_sector）；.TI 来自 ths mock
        conn.execute(
            "DELETE FROM dim_sector WHERE ts_code LIKE 'TEST_%'"
        )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_next_trade_date_holiday_skip() -> None:
    """next_trade_date 应跳过劳动节假期。"""
    from services.market.trade_date import next_trade_date
    _seed_fake_calendar()

    # 4/30(Thu) +1 → 5/6(Wed)：跳过 5/1~5/5 五连休
    got = next_trade_date("99990430", 1)
    assert got == "99990506", f"4/30 +1 期望 99990506，得到 {got}"
    # 4/30(Thu) +2 → 5/7(Thu)
    got2 = next_trade_date("99990430", 2)
    assert got2 == "99990507", f"4/30 +2 期望 99990507，得到 {got2}"
    # 5/8(Fri) +1 → 5/11(Mon)：跳过周末
    got3 = next_trade_date("99990508", 1)
    assert got3 == "99990511", f"5/8 +1 期望 99990511，得到 {got3}"


def case_02_trade_dates_between_range() -> None:
    """trade_dates_between [4/28, 5/11] 应返回 8 个开市日。"""
    from services.market.trade_date import trade_dates_between
    _seed_fake_calendar()
    opens = trade_dates_between("99990428", "99990511")
    expected = [
        "99990428", "99990429", "99990430",
        "99990506", "99990507", "99990508",
        "99990511",
    ]
    assert opens == expected, (
        f"区间开市日不匹配\n  expected: {expected}\n  actual:   {opens}"
    )


def case_03_sync_stock_daily_basic() -> None:
    """sync_stock_daily 应入库 mock 返回的 5400 行。"""
    from services.market.stock_daily_sync import sync_stock_daily
    _cleanup_fake_data()

    td = "99990506"
    mock_rows = [
        {
            "ts_code": f"TEST{i:04d}.SH",
            "trade_date": td,
            "open": 10.0, "high": 10.5, "low": 9.8,
            "close": 10.3, "pre_close": 10.0,
            "pct_chg": 3.0, "vol": 1000, "amount": 10300.0,
        }
        for i in range(5400)
    ]
    client = MockTushareClient({"daily": mock_rows})

    res = sync_stock_daily(td, client=client)
    assert res.ok, f"sync 失败: {res.error}"
    assert res.skipped is False
    assert res.rows_written == 5400, (
        f"rows_written 期望 5400，得到 {res.rows_written}"
    )
    assert res.api_calls == 1
    assert client.call_count == 1
    assert client.calls[0][0] == "daily"


def case_04_sync_stock_daily_idempotent() -> None:
    """重复调用 sync_stock_daily 应 skipped=True 且不再发 API。"""
    from services.market.stock_daily_sync import sync_stock_daily
    # case_03 已写过 5400 行 99990506 数据
    client = MockTushareClient({"daily": [{"x": 1}]})  # 即便给数据也不该调
    res = sync_stock_daily("99990506", client=client)
    assert res.ok, f"sync 失败: {res.error}"
    assert res.skipped is True, "幂等场景应 skipped=True"
    assert res.api_calls == 0, "幂等场景不应发起 API 调用"
    assert client.call_count == 0
    assert res.rows_written == 5400, (
        f"已存在行数应回传 5400，得到 {res.rows_written}"
    )


def case_05_sync_stock_daily_force() -> None:
    """force=True 应重新发 API 并先 DELETE 再 INSERT。"""
    from services.market.stock_daily_sync import sync_stock_daily
    # 给 mock 100 行，force 后表里只剩 100 行（不是 5400+100）
    mock_rows = [
        {
            "ts_code": f"TEST{i:04d}.SH",
            "trade_date": "99990506",
            "open": 20.0, "high": 21.0, "low": 19.5,
            "close": 20.5, "pre_close": 20.0,
            "pct_chg": 2.5, "vol": 500, "amount": 10250.0,
        }
        for i in range(100)
    ]
    client = MockTushareClient({"daily": mock_rows})
    res = sync_stock_daily("99990506", client=client, force=True)
    assert res.ok, f"sync 失败: {res.error}"
    assert res.skipped is False
    assert res.api_calls == 1
    assert res.rows_written == 100

    # 验库：99990506 当日总行数恰好 100
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM fact_stock_daily WHERE trade_date=?",
            ("99990506",),
        ).fetchone()["c"]
    assert n == 100, f"force 后行数应为 100，得到 {n}"


def case_06_sync_sector_daily_basic() -> None:
    """sync_sector_daily 多源调用：dc 三类 + ths 行业 + ths 概念，合计 580 行。

    2026-05-28 5 源改造后 + dc_index 4 字段扩充：
      - dc 概念 200 + dc 行业 200 + dc 地域 50
        + ths 行业 50 + ths 概念 80 = 580 行
      - 8 次 API 调用：moneyflow_ind_dc ×3 + moneyflow_ind_ths +
        moneyflow_cnt_ths + dc_index ×3（拉总市值/换手率/涨跌家数）

    注意：不能在此清 99990506 数据，case_07 还要用。
    """
    from services.market.sector_daily_sync import sync_sector_daily
    td = "99990507"

    def dc_mock(params: dict) -> List[dict]:
        ct = params.get("content_type", "")
        prefix_map = {"概念": "C", "行业": "I", "地域": "R"}
        count_map = {"概念": 200, "行业": 200, "地域": 50}
        prefix = prefix_map.get(ct, "X")
        count = count_map.get(ct, 0)
        return [
            {
                "ts_code": f"TEST_{prefix}{i:04d}.DC",
                "name": f"测试{ct}板块{i}",
                "pct_change": 1.5 + (i % 10) * 0.1,
                "net_amount": 1e8 * (i % 5),
                "buy_elg_amount": 5e7,
                "buy_lg_amount": 3e7,
                "rank": i + 1,
            }
            for i in range(count)
        ]

    def dc_index_mock(params: dict) -> List[dict]:
        """模拟 dc_index 接口：按 idx_type 区分，提供 total_mv 等 4 字段。"""
        idx = params.get("idx_type", "")
        # 与 dc_mock 同 ts_code，能 UPDATE 同 (trade_date, ts_code) 行
        prefix_map = {"概念板块": "C", "行业板块": "I", "地域板块": "R"}
        count_map = {"概念板块": 200, "行业板块": 200, "地域板块": 50}
        prefix = prefix_map.get(idx, "X")
        count = count_map.get(idx, 0)
        return [
            {
                "ts_code": f"TEST_{prefix}{i:04d}.DC",
                "name": f"测试{idx}{i}",
                "total_mv": 1e8 + i * 1e6,        # 万元
                "turnover_rate": 1.5 + (i % 10) * 0.1,
                "up_num": 10 + (i % 5),
                "down_num": 3 + (i % 4),
            }
            for i in range(count)
        ]

    ths_industry_mock = [
        {
            "ts_code": f"TEST_TI{i:04d}.TI",
            "industry": f"同花顺行业测试{i}",  # ⚠ 行业接口字段名 industry
            "pct_change": 0.5 + (i % 10) * 0.1,
            "net_amount": float(i % 50),  # ths 已是亿元
            "lead_stock": f"龙头I{i}",
            "company_num": 10 + i,
            "close": 1000 + i,
        }
        for i in range(50)
    ]
    ths_concept_mock = [
        {
            "ts_code": f"TEST_TC{i:04d}.TI",
            "name": f"同花顺概念测试{i}",  # ⚠ 概念接口字段名 name
            "pct_change": 0.3 + (i % 10) * 0.1,
            "net_amount": float(i % 30),
            "lead_stock": f"龙头C{i}",
            "company_num": 10 + i,
            "close_price": 100 + i,
        }
        for i in range(80)
    ]

    client = MockTushareClient({
        "moneyflow_ind_dc": dc_mock,
        "moneyflow_ind_ths": ths_industry_mock,
        "moneyflow_cnt_ths": ths_concept_mock,
        "dc_index": dc_index_mock,
    })

    res = sync_sector_daily(td, client=client)
    assert res.ok, f"sector sync 失败: {res.error}"
    assert res.rows_written == 580, (
        f"sector rows_written 期望 580（200+200+50+50+80），"
        f"得到 {res.rows_written}"
    )
    assert res.api_calls == 8, (
        f"api_calls 期望 8（dc 三类 + ths×2 + dc_index 三类）, "
        f"得到 {res.api_calls}"
    )
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        sample = conn.execute(
            "SELECT pct_chg, total_mv, turnover_rate, up_num, down_num "
            "FROM fact_sector_daily "
            "WHERE trade_date=? AND ts_code LIKE 'TEST_C%' LIMIT 1",
            (td,),
        ).fetchone()
    assert sample is not None and sample["pct_chg"] is not None, (
        f"pct_chg 入库为空: {dict(sample) if sample else None}"
    )
    # 新增 4 字段也要 UPDATE 成功（dc 源）
    assert sample["total_mv"] is not None, (
        "dc_index merge 失败：total_mv 应非空"
    )
    assert sample["turnover_rate"] is not None, "turnover_rate 应非空"
    assert sample["up_num"] is not None, "up_num 应非空"
    assert sample["down_num"] is not None, "down_num 应非空"


def case_08_sector_dual_source_idx_type() -> None:
    """5 源入库后，dim_sector 应同时有 5 种 idx_type 标记。

    针对 case_06 的副作用做更细粒度校验：每个子源的 idx_type/src 都正确写入。
    """
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute("""
            SELECT idx_type, src, COUNT(*) AS c
            FROM dim_sector
            WHERE ts_code LIKE 'TEST_%'
            GROUP BY idx_type, src
            ORDER BY c DESC
        """).fetchall()
    dist = {(r["idx_type"], r["src"]): r["c"] for r in rows}

    # 5 个子源：3 个 dc + 2 个 ths
    assert dist.get(("概念板块", "dc")) == 200, (
        f"概念板块/dc 期望 200，得到 {dist.get(('概念板块', 'dc'))}"
    )
    assert dist.get(("行业板块", "dc")) == 200, (
        f"行业板块/dc 期望 200，得到 {dist.get(('行业板块', 'dc'))}"
    )
    assert dist.get(("地域板块", "dc")) == 50, (
        f"地域板块/dc 期望 50，得到 {dist.get(('地域板块', 'dc'))}"
    )
    assert dist.get(("同花顺行业", "ths")) == 50, (
        f"同花顺行业/ths 期望 50，得到 {dist.get(('同花顺行业', 'ths'))}"
    )
    assert dist.get(("同花顺概念", "ths")) == 80, (
        f"同花顺概念/ths 期望 80，得到 {dist.get(('同花顺概念', 'ths'))}"
    )

    # ths ts_code 后缀必须是 .TI
    with db.connect(readonly=True) as conn:
        ths_codes = conn.execute(
            "SELECT ts_code FROM dim_sector WHERE src='ths' "
            "AND ts_code LIKE 'TEST_%' LIMIT 5"
        ).fetchall()
    assert all(r["ts_code"].endswith(".TI") for r in ths_codes), (
        f"ths ts_code 后缀应为 .TI，得到 {[r['ts_code'] for r in ths_codes]}"
    )

    # ths 资金流字段：main_net_yi 直填，elg/lg 为 NULL
    with db.connect(readonly=True) as conn:
        ths_sample = conn.execute("""
            SELECT main_net_yi, main_elg_yi, main_lg_yi, rank_today
            FROM fact_sector_daily
            WHERE trade_date='99990507'
              AND ts_code LIKE 'TEST_TI%' LIMIT 1
        """).fetchone()
    assert ths_sample is not None, "ths 样本应入库"
    assert ths_sample["main_elg_yi"] is None, "ths 的 elg 字段应为 NULL"
    assert ths_sample["main_lg_yi"] is None, "ths 的 lg 字段应为 NULL"
    assert ths_sample["rank_today"] is None, "ths 无 rank 字段，应为 NULL"

    # ths 概念字段名差异：moneyflow_cnt_ths 用 name 字段
    # 校验概念也正确入库（数据库里只能查 dim_sector.name，应为"同花顺概念测试X"）
    with db.connect(readonly=True) as conn:
        concept_name = conn.execute(
            "SELECT name FROM dim_sector WHERE ts_code LIKE 'TEST_TC%' LIMIT 1"
        ).fetchone()
    assert concept_name is not None, "ths 概念应入 dim_sector"
    assert "同花顺概念测试" in concept_name["name"], (
        f"ths 概念 name 字段映射错误，得到 {concept_name['name']!r}"
    )


def case_07_cross_db_join_smoke() -> None:
    """attached_dbs 从 ai_inference 跨库查 market.fact_stock_daily 不应抛错。"""
    from services.storage.cross_db import attached_dbs
    # case_05 留下 99990506 stock 100 行；case_06 留下 99990507 sector 500 行
    with attached_dbs(
        primary="ai", attach=("market",), readonly=True,
    ) as conn:
        row = conn.execute(
            "SELECT ts_code, trade_date, pct_chg "
            "FROM market.fact_stock_daily "
            "WHERE trade_date = '99990506' LIMIT 1"
        ).fetchone()
        sector_n = conn.execute(
            "SELECT COUNT(*) AS c FROM market.fact_sector_daily "
            "WHERE trade_date = '99990507'"
        ).fetchone()["c"]
    assert row is not None, "跨库 SELECT 应该能查到 99990506 的 stock 数据"
    assert str(row["trade_date"]) == "99990506"
    assert sector_n == 580, (
        f"跨库 sector COUNT 期望 580（5 源 mock 总和），得到 {sector_n}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_next_trade_date_holiday_skip", case_01_next_trade_date_holiday_skip),
    ("02_trade_dates_between_range", case_02_trade_dates_between_range),
    ("03_sync_stock_daily_basic", case_03_sync_stock_daily_basic),
    ("04_sync_stock_daily_idempotent", case_04_sync_stock_daily_idempotent),
    ("05_sync_stock_daily_force", case_05_sync_stock_daily_force),
    ("06_sync_sector_daily_basic", case_06_sync_sector_daily_basic),
    ("07_cross_db_join_smoke", case_07_cross_db_join_smoke),
    ("08_sector_dual_source_idx_type", case_08_sector_dual_source_idx_type),
]


def main() -> int:
    print(f"=== Phase 1 行情同步回归 ({len(_CASES)} 用例) ===\n")
    passed = []
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
        print("\n[cleanup] 已删除所有 9999*/TEST_* 伪数据")

    print(f"\n[全部通过] {len(passed)}/{len(_CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
